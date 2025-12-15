from fastapi import APIRouter, Request, Depends, Form, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, or_, cast, String, not_
from fastapi.responses import HTMLResponse, JSONResponse
from datetime import datetime, timedelta
import pytz
import json
from typing import Optional
import re
# Imports do seu projeto
from database import get_db
# AQUI ESTAVA O ERRO: Faltava TransactionType e EmployeeTransaction
from models import User, Order, DriverSession, DriverAdvance, CashClosing, CashOpening, CashTransaction, EmployeeTransaction, TransactionType, Bill
from auth import verify_password
from dependencies import templates, check_db_auth, check_role
from services.sockets import manager
from services.utils import get_br_time

router = APIRouter()

def get_today_range():
    tz = pytz.timezone('America/Sao_Paulo')
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(pytz.utc)
    end = now.replace(hour=23, minute=59, second=59, microsecond=999999).astimezone(pytz.utc)
    return start, end

# --- VIEW: TELA PRINCIPAL ---
@router.get("/admin/management", response_class=HTMLResponse)
def management_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role(["owner", "manager"]))
):
    now_br = get_br_time()
    
    # Busca Abertura e Fechamento mais recentes
    opening = db.query(CashOpening).filter(
        CashOpening.store_id == current_user.store_id
    ).order_by(desc(CashOpening.created_at)).first()
    
    closing = db.query(CashClosing).filter(
        CashClosing.store_id == current_user.store_id
    ).order_by(desc(CashClosing.closed_at)).first()

    status_caixa = "FECHADO"
    
    # Lógica de Status
    if opening:
        if not closing:
            status_caixa = "ABERTO"
        elif opening.created_at > closing.closed_at:
            status_caixa = "ABERTO"
        else:
            status_caixa = "FECHADO"
    
    # === RECUPERA O FUNDO DE CAIXA DEIXADO (TROCO) ===
    suggested = 0.0
    if closing and hasattr(closing, 'next_opening_amount'):
        suggested = closing.next_opening_amount or 0.0
    # =================================================

    drivers = db.query(User).filter(
        User.store_id == current_user.store_id, 
        User.role == 'driver'
    ).all()

    return templates.TemplateResponse("management.html", {
        "request": request,
        "current_user": current_user,
        "drivers": drivers,
        "box_state": {
            "status": status_caixa, 
            "opening_value": opening.amount if opening else 0.0,
            "suggested_opening": suggested # O HTML vai ler isso aqui
        }
    })

# --- API: RESUMO DO CAIXA ---

@router.get("/admin/api/finance/daily-summary")
def get_daily_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    store_id = current_user.store_id
    
    # 1. Busca a última abertura e fechamento
    last_opening = db.query(CashOpening).filter(CashOpening.store_id == store_id).order_by(desc(CashOpening.created_at)).first()
    last_closing = db.query(CashClosing).filter(CashClosing.store_id == store_id).order_by(desc(CashClosing.closed_at)).first()
    
    is_open = False
    start_filter = None
    opening_val = 0.0
    current_cash_id = None

    if last_opening:
        # Se fechamento > abertura, está fechado.
        if last_closing and last_closing.closed_at > last_opening.created_at:
            is_open = False
        else:
            is_open = True
            start_filter = last_opening.created_at
            opening_val = last_opening.amount
            current_cash_id = last_opening.id

    # 2. Inicializa Zerado
    summary = {
        "opening_value": opening_val if is_open else 0.0,
        "suplementos": 0.0,
        "sangrias": 0.0,
        "total_revenue": 0.0,
        "total_pending": 0.0,
        "sum_money": 0.0,
        "sum_pix": 0.0,
        "sum_card": 0.0,
        "sum_ifood": 0.0,   
        "sum_virtual": 0.0,
        "sum_others": 0.0,
        "expected_in_box": 0.0,
        "is_box_open": is_open
    }

    # SE CAIXA FECHADO, RETORNA TUDO ZERADO
    if not is_open:
        return summary 

    # 3. Busca Transações (CORRIGIDO)
    # Tenta usar o vínculo forte (cash_opening_id) ou data como fallback
    trans_query = db.query(CashTransaction).filter(CashTransaction.store_id == store_id)
    
    # --- AQUI ESTAVA O ERRO ---
    # Antes estava chamando 'Order' e 'orders_query'
    if current_cash_id and hasattr(CashTransaction, 'cash_opening_id'):
        trans_query = trans_query.filter(CashTransaction.cash_opening_id == current_cash_id)
    else:
        trans_query = trans_query.filter(CashTransaction.created_at >= start_filter)
    # --------------------------
         
    trans = trans_query.all()
    
    total_suplemento = sum(t.amount for t in trans if t.type == 'SUPLEMENTO')
    total_sangria = sum(t.amount for t in trans if t.type in ['SANGRIA', 'PAGAMENTO_MOTOBOY'])
    
    # Fiados Recebidos
    fiado_money = sum(t.amount for t in trans if t.type == 'RECEBIMENTO_FIADO_DINHEIRO')
    fiado_pix = sum(t.amount for t in trans if t.type == 'RECEBIMENTO_FIADO_PIX')
    fiado_card = sum(t.amount for t in trans if t.type == 'RECEBIMENTO_FIADO_CARTAO')

    summary["suplementos"] = total_suplemento
    summary["sangrias"] = total_sangria

    # 4. Busca Pedidos (Só se aberto)
    orders_query = db.query(Order).filter(Order.store_id == store_id)
    if current_cash_id and hasattr(Order, 'cash_opening_id'):
        orders_query = orders_query.filter(
            or_(Order.cash_opening_id == current_cash_id, Order.created_at >= start_filter)
        )
    else:
        orders_query = orders_query.filter(Order.created_at >= start_filter)
        
    orders = orders_query.all()
    
    for o in orders:
        if o.status and 'CANCELADO' in o.status: continue
        val = o.total_value or 0.0
        method = (o.payment_method or "").lower().strip()
        
        if not method or "em aberto" in method:
            summary["total_pending"] += val
            continue
        
        summary["sum_money"] += fiado_money
        summary["sum_pix"] += fiado_pix
        summary["sum_card"] += fiado_card
        summary["total_revenue"] += (fiado_money + fiado_pix + fiado_card) 
        
        # --- LÓGICA DE CLASSIFICAÇÃO ATUALIZADA ---
        if "ifood online" in method or "site mercadopago" in method:
            summary["sum_ifood"] += val
            summary["total_revenue"] += val
        
        # Detecta Conta Funcionário / Consumo Interno
        elif "conta func" in method or "consumo" in method: # <--- NOVO BLOCO
            summary["sum_virtual"] += val
            summary["total_revenue"] += val
            
        elif "dinheiro" in method:
            summary["sum_money"] += val
            summary["total_revenue"] += val
            
        elif "pix" in method:
            summary["sum_pix"] += val
            summary["total_revenue"] += val
            
        elif any(x in method for x in ["cart", "crédito", "credito", "débito", "debito"]):
            summary["sum_card"] += val
            summary["total_revenue"] += val
            
        else:
            summary["sum_others"] += val
            summary["total_revenue"] += val
            
        # Reseta os fiados para não somar repetido a cada loop
        fiado_money = 0; fiado_pix = 0; fiado_card = 0

    # --- CÁLCULO FINAL DA GAVETA ---
    summary["expected_in_box"] = (summary["opening_value"] + summary["suplementos"] + summary["sum_money"]) - summary["sangrias"]
    
    return summary



# --- API: LISTA INFINITA ---
@router.get("/admin/api/finance/orders/list")
def get_finance_orders_history(
    page: int = 1,
    limit: int = 20,
    search: Optional[str] = None,
    driver_id: Optional[int] = None,
    box_status: Optional[str] = Query('open'), # Mantido para compatibilidade
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    store_id = current_user.store_id
    
    # ======================================================================
    # 1. VERIFICAÇÃO RIGOROSA DO CAIXA
    # ======================================================================
    # Busca a última abertura e o último fechamento
    last_opening = db.query(CashOpening).filter(
        CashOpening.store_id == store_id
    ).order_by(desc(CashOpening.created_at)).first()
    
    last_closing = db.query(CashClosing).filter(
        CashClosing.store_id == store_id
    ).order_by(desc(CashClosing.closed_at)).first()
    
    is_open = False
    current_cash_id = None
    start_filter = datetime.now() # Placeholder de segurança

    if last_opening:
        # Lógica: Se tem fechamento posterior à abertura, está FECHADO
        if last_closing and last_closing.closed_at > last_opening.created_at:
            is_open = False
        else:
            is_open = True
            start_filter = last_opening.created_at
            current_cash_id = last_opening.id

    # ======================================================================
    # 2. SE CAIXA FECHADO -> RETORNA TUDO ZERADO
    # ======================================================================
    if not is_open:
        return {
            "orders": [],
            "has_more": False,
            "meta": {
                "total_value": 0.0,
                "count": 0
            }
        }

    # ======================================================================
    # 3. QUERY BLINDADA (CAIXA ABERTO)
    # ======================================================================
    query = db.query(Order).filter(Order.store_id == store_id)
    
    # Filtro Híbrido: Tenta usar o vínculo forte (ID) ou Data como fallback
    if current_cash_id and hasattr(Order, 'cash_opening_id'):
        query = query.filter(
            or_(
                Order.cash_opening_id == current_cash_id, # Pedidos vinculados a este caixa
                Order.created_at >= start_filter          # + Pedidos novos sem vínculo (segurança)
            )
        )
    else:
        # Se não rodou a migração da coluna nova, usa apenas o horário
        query = query.filter(Order.created_at >= start_filter)

    # Filtros Opcionais (Busca e Motoboy)
    if driver_id:
        query = query.filter(Order.driver_id == driver_id)

    if search and search.strip():
        term = f"%{search}%"
        query = query.filter(or_(
            Order.customer_name.ilike(term),
            Order.wabiz_id.ilike(term),
            cast(Order.id, String).ilike(term)
        ))

    # Paginação e Ordenação
    total_count = query.count()
    orders = query.order_by(desc(Order.created_at)).offset((page-1)*limit).limit(limit).all()
    
    # Serialização dos Dados
    data = []
    total_val_visual = 0.0

    for o in orders:
        # Soma no total visual se não for cancelado
        is_canceled = o.status and 'CANCELADO' in o.status
        if not is_canceled:
            total_val_visual += (o.total_value or 0.0)
            
        data.append({
            "id": o.id,
            "wabiz_id": o.wabiz_id or str(o.id),
            "name": o.customer_name,
            "phone": o.customer_phone,
            "total": o.total_value,
            "status": o.status, 
            "payment": o.payment_method,
            "time": o.created_at.strftime('%d/%m %H:%M'),
            "items": o.items_json,
            "delivery_type": "Mesa" if o.table_number else "Delivery/Balcão",
            "address": o.address_street or "Retirada",
            "driver_name": o.driver.full_name if o.driver else "Sem Motoboy",
            "discount": o.discount,
            "delivery_fee": o.delivery_fee
        })

    return {
        "orders": data,
        "has_more": (page * limit) < total_count,
        "meta": {
            "total_value": total_val_visual,
            "count": total_count
        }
    }
    

# --- API: LISTA MOTOBOYS (A QUE DAVA 404) ---
@router.get("/admin/api/finance/drivers-live")
def get_drivers_live_metrics(
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    store_id = current_user.store_id
    
    # 1. VERIFICAÇÃO DE CAIXA
    last_opening = db.query(CashOpening).filter(CashOpening.store_id == store_id).order_by(desc(CashOpening.created_at)).first()
    last_closing = db.query(CashClosing).filter(CashClosing.store_id == store_id).order_by(desc(CashClosing.closed_at)).first()
    
    current_cash_id = None
    
    if last_opening:
        if not last_closing or last_opening.created_at > last_closing.closed_at:
            current_cash_id = last_opening.id

    drivers = db.query(User).filter(User.store_id == store_id, User.role == 'driver').all()
    final_list = []
    
    for d in drivers:
        active_session = db.query(DriverSession).filter(
            DriverSession.driver_id == d.id, DriverSession.end_time == None
        ).first()

        # SE CAIXA FECHADO -> TUDO ZERO
        if not current_cash_id:
            final_list.append({
                "id": d.id, "name": d.full_name.split()[0], "avatar": d.full_name[:2].upper(),
                "status": "Offline", "deliveries": 0, "total_fees": 0.0, "total_tips": 0.0,
                "total_advances": 0.0, "final_pay": 0.0, "is_champion": False
            })
            continue 

        # === CÁLCULO BLINDADO (Igual ao Modal) ===

        # 1. Pedidos PENDENTES de pagamento (Novo Ciclo)
        orders = db.query(Order).filter(
            Order.driver_id == d.id,
            Order.status.in_(['ENTREGUE', 'CONCLUIDO']),
            Order.cash_opening_id == current_cash_id,
            Order.is_driver_paid == False # <--- Apenas o que não foi baixado
        ).all()
        
        # 2. Vales/Adiantamentos PENDENTES
        raw_advs = db.query(DriverAdvance).join(DriverSession).filter(
            DriverSession.driver_id == d.id,
            DriverSession.cash_opening_id == current_cash_id,
            DriverAdvance.is_paid == False # <--- Apenas o que não foi baixado
        ).all()
        
        # 3. Filtra Zumbis (Sessões antigas fechadas)
        valid_credits = 0.0 # Diária + Bónus
        valid_debits = 0.0  # Vales + Restos de Dívida
        
        for item in raw_advs:
            # Se sessão fechada e não é resto -> Ignora
            if item.session.end_time is not None:
                if "Resto" not in (item.reason or ""):
                    continue 
            
            if item.amount < 0:
                valid_credits += abs(item.amount)
            else:
                valid_debits += item.amount
        
        # 4. Totais
        total_fees = sum(o.delivery_fee or 0.0 for o in orders)
        total_tips = sum(o.driver_tip or 0.0 for o in orders)
        
        # Saldo Final = (Ganhos + Créditos) - Débitos
        final_pay = (total_fees + total_tips + valid_credits) - valid_debits

        # Contagem de entregas considera TUDO do caixa (para estatística), não só o pendente
        # Mas se preferir alinhar com o saldo, pode usar len(orders)
        # Vamos manter len(orders) para ser consistente: entregas a receber.
        
        final_list.append({
            "id": d.id,
            "name": d.full_name.split()[0],
            "avatar": d.full_name[:2].upper(),
            "status": "Online" if active_session else "Offline",
            "deliveries": len(orders), 
            "total_fees": total_fees,
            "total_tips": total_tips,
            "total_advances": valid_debits,
            "final_pay": final_pay,
            "is_champion": False
        })

    if final_list:
        champion = max(final_list, key=lambda x: x['deliveries'])
        if champion['deliveries'] > 0: champion['is_champion'] = True

    return sorted(final_list, key=lambda x: (x['status'] == 'Online', x['deliveries']), reverse=True)

# --- API: DETALHE MOTOBOY ---
@router.get("/admin/api/finance/driver/{driver_id}/summary")
def get_driver_summary(
    driver_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    # Identifica Caixa Aberto
    last_opening = db.query(CashOpening).filter(CashOpening.store_id == current_user.store_id).order_by(desc(CashOpening.created_at)).first()
    last_closing = db.query(CashClosing).filter(CashClosing.store_id == current_user.store_id).order_by(desc(CashClosing.closed_at)).first()
    
    current_cash_id = None
    if last_opening:
        if not last_closing or last_opening.created_at > last_closing.closed_at:
            current_cash_id = last_opening.id

    driver = db.query(User).get(driver_id)
    
    if not current_cash_id:
        return {
            "name": driver.full_name, "fixed_fee": 0.0, "deliveries": 0, "total_fees": 0.0,
            "total_tips": 0.0, "total_advances": 0.0, "previous_balance": 0.0, "final_due": 0.0, "history": []
        }

    # 1. BUSCA TUDO DO CAIXA
    all_items = db.query(DriverAdvance).join(DriverSession).filter(
        DriverSession.cash_opening_id == current_cash_id,
        DriverSession.driver_id == driver_id
    ).order_by(DriverAdvance.created_at).all()

    # Busca a sessão ATIVA para filtrar os pedidos
    active_session = db.query(DriverSession).filter(
        DriverSession.driver_id == driver_id,
        DriverSession.end_time == None,
        DriverSession.cash_opening_id == current_cash_id
    ).first()
    
    # Se não tem sessão ativa, filtra por agora (segurança)
    start_filter = active_session.start_time if active_session else datetime.now()

    # Pedidos apenas do NOVO CICLO (Sessão Atual)
    all_orders = db.query(Order).filter(
        Order.driver_id == driver_id,
        Order.status.in_(['ENTREGUE', 'CONCLUIDO']),
        Order.cash_opening_id == current_cash_id,
        Order.created_at >= start_filter 
    ).all()

    # --- 2. CÁLCULO FINANCEIRO ---
    
    active_fees = sum(o.delivery_fee or 0.0 for o in all_orders if not o.is_driver_paid)
    active_tips_orders = sum(o.driver_tip or 0.0 for o in all_orders if not o.is_driver_paid)
    
    manual_bonus_val = 0.0
    sum_debits_val = 0.0
    diaria_credit = 0.0
    
    # Filtra e soma apenas itens válidos para o ciclo
    for item in all_items:
        # Ignora itens de sessões FECHADAS, exceto se for "Resto" (Dívida)
        if item.session.end_time is not None:
            if "Resto" not in (item.reason or ""):
                continue

        if item.reason == 'DIARIA_AUTO':
            if not item.is_paid: diaria_credit += abs(item.amount)
            continue
            
        if item.amount < 0:
            if not item.is_paid: manual_bonus_val += abs(item.amount) # Crédito
        else:
            if not item.is_paid: sum_debits_val += item.amount # Débito

    total_tips = active_tips_orders + manual_bonus_val
    final_due = (active_fees + total_tips + diaria_credit) - sum_debits_val

    # --- 3. VISUALIZAÇÃO (HISTÓRICO SINCRONIZADO) ---
    transactions_list = []
    
    fixed_fee_display = diaria_credit
    
    tz_br = pytz.timezone('America/Sao_Paulo')
    
    for item in all_items:
        # A. Oculta Diária da lista
        if item.reason == 'DIARIA_AUTO': continue 
        
        # B. Oculta Caixinha do topo
        is_current_cycle = (item.session.end_time is None)
        if item.amount < 0 and not item.is_paid and is_current_cycle: continue
        
        # C. FILTRO VISUAL DE CICLO (IGUAL AO CÁLCULO)
        # Esconde transações de sessões fechadas que não são Restos
        if item.session.end_time is not None:
             if "Resto" not in (item.reason or ""):
                 continue
        
        t_str = "--:--"
        try: t_str = item.created_at.astimezone(tz_br).strftime("%H:%M")
        except: pass
        
        reason_txt = item.reason or "Sem descrição"
        if item.is_paid:
            reason_txt += " (Pago)"
        
        transactions_list.append({
            "time": t_str, "amount": item.amount, "reason": reason_txt, "order_id": None
        })

    return {
        "name": driver.full_name, 
        "fixed_fee": fixed_fee_display, 
        "deliveries": len(all_orders),
        "total_fees": active_fees,
        "total_tips": total_tips, 
        "total_advances": sum_debits_val,
        "previous_balance": 0.0,
        "final_due": final_due, 
        "history": transactions_list
    }

# --- AÇÕES DE ESCRITA (POST) ---

@router.post("/admin/api/finance/driver/advance")
def add_driver_advance(
    driver_id: int = Form(...),
    amount: float = Form(...),
    reason: str = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role(["owner", "manager"]))
):
    # Cria sessão silenciosa se não tiver
    session = db.query(DriverSession).filter(DriverSession.driver_id == driver_id, DriverSession.end_time == None).first()
    if not session:
        session = DriverSession(driver_id=driver_id, store_id=current_user.store_id)
        db.add(session)
        db.commit()

    adv = DriverAdvance(session_id=session.id, amount=amount, reason=reason)
    db.add(adv)
    db.commit()
    return {"success": True}

@router.post("/admin/api/finance/driver/settle")
def settle_driver(
    driver_id: int = Form(...),
    amount_paid: float = Form(...),
    payment_method: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role(["owner", "manager"]))
):
    # 1. Identifica Caixa Aberto para vincular a transação
    last_opening = db.query(CashOpening).filter(CashOpening.store_id == current_user.store_id).order_by(desc(CashOpening.created_at)).first()
    last_closing = db.query(CashClosing).filter(CashClosing.store_id == current_user.store_id).order_by(desc(CashClosing.closed_at)).first()
    
    current_cash_id = None
    if last_opening:
        if not last_closing or last_opening.created_at > last_closing.closed_at:
            current_cash_id = last_opening.id

    # 2. Busca sessão ativa para registrar o adiantamento
    active_session = db.query(DriverSession).filter(
        DriverSession.driver_id == driver_id,
        DriverSession.end_time == None
    ).first()

    driver = db.query(User).get(driver_id)
    
    # 3. Registra como "Adiantamento/Baixa" na sessão
    if active_session:
        advance = DriverAdvance(
            session_id=active_session.id,
            amount=amount_paid, # Positivo = Débito (pois pagamos ao motoboy)
            reason=f"Pagamento/Baixa ({payment_method})",
            is_paid=False # Entra na conta do fechamento
        )
        db.add(advance)

    # 4. Registra saída do Caixa (CORRIGIDO COM VÍNCULO)
    if payment_method == 'DINHEIRO':
        op = CashTransaction(
            store_id=current_user.store_id,
            user_id=current_user.id,
            type='PAGAMENTO_MOTOBOY',
            amount=amount_paid,
            description=f"Pagamento {driver.full_name}",
            cash_opening_id=current_cash_id # <--- VÍNCULO IMPORTANTE
        )
        db.add(op)

    db.commit()
    
    # Para recalcular o saldo visual, chamamos a lógica de resumo
    # (Poderíamos chamar get_driver_summary aqui, mas vamos retornar simples)
    return {"success": True}


@router.post("/admin/api/finance/cash/operation")
def cash_operation(
    type: str = Form(...),
    amount: float = Form(...),
    reason: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role(["owner", "manager"]))
):
    # 1. Busca o Caixa Aberto (Lógica Padrão de Blindagem)
    last_opening = db.query(CashOpening).filter(
        CashOpening.store_id == current_user.store_id
    ).order_by(desc(CashOpening.created_at)).first()
    
    last_closing = db.query(CashClosing).filter(
        CashClosing.store_id == current_user.store_id
    ).order_by(desc(CashClosing.closed_at)).first()
    
    current_cash_id = None
    
    # Se tem abertura E (não tem fechamento OU a abertura é mais recente)
    if last_opening:
        if not last_closing or last_opening.created_at > last_closing.closed_at:
            current_cash_id = last_opening.id
    
    # Se tentar lançar sangria com caixa fechado, avisa ou permite (mas sem ID)
    if not current_cash_id:
        # Opção A: Bloquear (Recomendado para rigor)
        # return JSONResponse(status_code=400, content={"message": "Abra o caixa antes de lançar movimentações!"})
        
        # Opção B: Permitir (Vai ficar 'sem dono', cuidado!)
        pass

    op = CashTransaction(
        store_id=current_user.store_id,
        user_id=current_user.id,
        type=type,
        amount=amount,
        description=reason,
        cash_opening_id=current_cash_id # <--- GRAVA O VÍNCULO AQUI
    )
    db.add(op)
    db.commit()
    
    return {"success": True}



@router.post("/admin/api/finance/open-box")
async def open_cash_box(
    amount: float = Form(0.0),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role(["owner", "manager"]))
):
    # 1. Verifica se já está aberto (Código existente)
    last_opening = db.query(CashOpening).filter(CashOpening.store_id == current_user.store_id).order_by(desc(CashOpening.created_at)).first()
    last_closing = db.query(CashClosing).filter(CashClosing.store_id == current_user.store_id).order_by(desc(CashClosing.closed_at)).first()

    if last_opening and (not last_closing or last_opening.created_at > last_closing.closed_at):
        return JSONResponse(status_code=400, content={"message": "Caixa já aberto!"})
    
    # 2. Abre o Novo Caixa

    new_opening = CashOpening(
        store_id=current_user.store_id, 
        user_id=current_user.id, 
        amount=amount,
    )
    db.add(new_opening)
    db.commit() # Comita para gerar o ID
    
    # ======================================================================
    # 🧹 FAXINA AUTOMÁTICA (LÓGICA BLINDADA)
    # ======================================================================
    try:
        # Lógica Inteligente:
        # Se houve um fechamento recente, pegamos apenas os órfãos nascidos DEPOIS dele.
        # Se é a primeira vez (ou faz muito tempo), usamos uma margem de segurança de 12h.
        
        if last_closing:
            limit_date = last_closing.closed_at
        else:
            limit_date = now_br - timedelta(hours=12) # Margem segura se não tiver histórico
        
        # Busca pedidos "órfãos" (sem caixa definido) APENAS do intervalo válido
        orphans_count = db.query(Order).filter(
            Order.store_id == current_user.store_id,
            Order.cash_opening_id == None, # Sem dono
            Order.created_at >= limit_date # Apenas novos desde o último fechamento
        ).update({Order.cash_opening_id: new_opening.id}, synchronize_session=False)
        
        drivers_count = db.query(DriverSession).filter(
            DriverSession.store_id == current_user.store_id,
            DriverSession.cash_opening_id == None,
            DriverSession.end_time == None # Apenas sessões ativas
        ).update({DriverSession.cash_opening_id: new_opening.id}, synchronize_session=False)
        
        db.commit()
        print(f"💰 Caixa Aberto! {orphans_count} pedidos e {drivers_count} motoboys vinculados.")
        
    except Exception as e:
        print(f"⚠️ Erro ao resgatar pedidos órfãos: {e}")
        # Não trava a abertura se der erro aqui, apenas loga
    # ======================================================================
    
    await manager.broadcast(current_user.store_id, "cash_update")
    
    return {"success": True, "message": f"Caixa aberto com R$ {amount:.2f}!"}

@router.post("/admin/api/finance/close-box")
async def close_cash_register(
    real_values: str = Form(...),
    notes: str = Form(None),
    closer_name: str = Form(...),
    leftover: float = Form(0.0),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role(["owner", "manager"]))
):
    # ==============================================================================
    # 1. BLOQUEIO: PEDIDOS EM ABERTO (Lista Detalhada)
    # ==============================================================================
    blocking_orders = db.query(Order).filter(
        Order.store_id == current_user.store_id,
        not_(Order.status.in_(['ENTREGUE', 'CONCLUIDO'])),
        not_(Order.status.ilike('%CANCELADO%'))
    ).all()

    if blocking_orders:
        # Ex: "#101 (Maria), #102 (João)"
        details = ", ".join([f"#{o.wabiz_id or o.id} ({o.customer_name or 'Cli'})" for o in blocking_orders[:3]])
        if len(blocking_orders) > 3:
            details += f" e mais {len(blocking_orders) - 3}..."
            
        return JSONResponse(
            status_code=400, 
            content={"message": f"Impossível fechar! Finalize estes pedidos: {details}"}
        )

    # ==============================================================================
    # 2. BLOQUEIO: MOTOBOYS COM TURNO ABERTO (Lista Detalhada)
    # ==============================================================================
    blocking_drivers = db.query(DriverSession).join(User).filter(
        DriverSession.store_id == current_user.store_id,
        DriverSession.end_time == None
    ).all()

    if blocking_drivers:
        # Ex: "João, Pedro"
        names = ", ".join([s.driver.full_name.split()[0] for s in blocking_drivers[:3]])
        if len(blocking_drivers) > 3:
            names += f" e mais {len(blocking_drivers) - 3}..."
        
        return JSONResponse(
            status_code=400, 
            content={"message": f"Impossível fechar! Faça o acerto destes motoboys: {names}"}
        )

    # ==============================================================================
    # 3. BLOQUEIO: CONTAS A PAGAR VENCENDO HOJE (Lista Detalhada)
    # ==============================================================================
    now_br = get_br_time()
    end_of_day = now_br.replace(hour=23, minute=59, second=59)

    blocking_bills = db.query(Bill).filter(
        Bill.store_id == current_user.store_id,
        Bill.due_date <= end_of_day, # Vence hoje ou antes
        Bill.paid_at == None         # Não foi paga
    ).all()

    if blocking_bills:
        # Ex: "Luz (R$ 150.00), Fornecedor (R$ 500.00)"
        bills_desc = ", ".join([f"{b.description} (R$ {b.amount:.2f})" for b in blocking_bills[:3]])
        if len(blocking_bills) > 3:
            bills_desc += f" e mais {len(blocking_bills) - 3}..."

        return JSONResponse(
            status_code=400,
            content={"message": f"Impossível fechar! Contas pendentes vencendo hoje: {bills_desc}. Dê baixa ou mude a data."}
        )

    # ==============================================================================
    # CONTINUAÇÃO NORMAL DO FECHAMENTO...
    # ==============================================================================
    
    # === CÁLCULO FINANCEIRO CORRIGIDO ===
    real_dict = json.loads(real_values)
    summary = get_daily_summary(db, current_user)
    
    # O que o usuário contou (Dinheiro + Cartão + Pix)
    total_real = sum(float(v) for v in real_dict.values())
    
    # O que o sistema espera (Vendas Totais + Fundo Abertura + Suplementos - Sangrias)
    total_revenue = summary.get('total_revenue', 0.0)
    opening_val = summary.get('opening_value', 0.0)
    sups = summary.get('suplementos', 0.0)
    sangs = summary.get('sangrias', 0.0)
    
    # CORREÇÃO CRÍTICA: Removemos pagamentos digitais (iFood Online) da expectativa da gaveta
    # Se não fizermos isso, o sistema acusa "FALTA" porque esse dinheiro não está lá fisicamente.
    digital_payments = summary.get('sum_ifood', 0.0)
    virtual_payments = summary.get('sum_virtual', 0.0)
    
    total_system = (total_revenue - digital_payments - virtual_payments) + opening_val + sups - sangs
    
    diff = total_real - total_system
    
    # Pequena tolerância para arredondamento (centavos)
    if abs(diff) < 0.02: 
        diff = 0.0
        
    last_opening_obj = db.query(CashOpening).filter(
        CashOpening.store_id == current_user.store_id
    ).order_by(desc(CashOpening.created_at)).first()
    
    opened_at_val = last_opening_obj.created_at if last_opening_obj else datetime.now()

    closing = CashClosing(
        store_id=current_user.store_id,
        user_id=current_user.id,
        closer_name=closer_name,
        total_system=total_system,
        total_real=total_real,
        difference=diff,
        breakdown_json=real_dict,
        notes=notes,
        next_opening_amount=leftover,
        opened_at=opened_at_val # <--- ADICIONADO: Salva quando o caixa abriu
    )
    db.add(closing)
    db.commit()
    
    # 4. Alerta WhatsApp (Mantido)
    if abs(diff) > 0.50:
        try:
            owner = db.query(User).filter(User.store_id == current_user.store_id, User.role == 'owner').first()
            store = current_user.store
            if owner and store.whatsapp_api_token:
                tipo = "SOBRA" if diff > 0 else "FALTA"
                task_send_whatsapp.delay(
                    phone=store.whatsapp_number,
                    template_name="admin_alert_v1",
                    variables=[closer_name, f"R$ {diff:.2f}", "Fechamento de Caixa"],
                    store_token=store.whatsapp_api_token,
                    phone_id=store.whatsapp_phone_id
                )
        except: pass

    await manager.broadcast(current_user.store_id, "cash_update")
    
    # Mensagem de Retorno Inteligente
    msg_diff = ""
    if diff != 0:
        status_diff = "SOBRA" if diff > 0 else "FALTA"
        msg_diff = f" (Quebra: {status_diff} de R$ {abs(diff):.2f})"

    return {
        "success": True, 
        "diff": diff, 
        "message": f"Caixa fechado.{msg_diff} Fundo reservado: R$ {leftover:.2f}"
    }


@router.get("/admin/api/finance/closings")
def get_closings_history(
    start: Optional[str] = None,
    end: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    query = db.query(CashClosing).filter(CashClosing.store_id == current_user.store_id)
    
    if start:
        dt_start = datetime.strptime(start, '%Y-%m-%d').replace(hour=0, minute=0, second=0)
        query = query.filter(CashClosing.closed_at >= dt_start)
    
    if end:
        dt_end = datetime.strptime(end, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
        query = query.filter(CashClosing.closed_at <= dt_end)
        
    closings = query.order_by(desc(CashClosing.closed_at)).limit(50).all()
    
    return [{
        "id": c.id,
        "date_fmt": c.closed_at.strftime('%d/%m/%Y'),
        "time_fmt": c.closed_at.strftime('%H:%M'),
        "closer": c.closer_name,
        "total_real": c.total_real,
        "diff": c.difference,
        "status": "OK" if abs(c.difference) < 1.0 else ("SOBRA" if c.difference > 0 else "FALTA")
    } for c in closings]

@router.get("/admin/api/finance/closings/{closing_id}/details")
def get_closing_details_full(
    closing_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    closing = db.query(CashClosing).get(closing_id)
    if not closing or closing.store_id != current_user.store_id:
        return JSONResponse(status_code=404, content={"message": "Não encontrado"})
    
    # Define Janela de Tempo
    start_time = closing.opened_at or (closing.closed_at - timedelta(hours=12))
    end_time = closing.closed_at

    # 0. BUSCA VALOR DE ABERTURA (Para cálculo do Dinheiro Esperado)
    opening_val = 0.0
    if closing.opened_at:
        opening_obj = db.query(CashOpening).filter(
            CashOpening.store_id == current_user.store_id,
            CashOpening.created_at == closing.opened_at
        ).first()
        if opening_obj: opening_val = opening_obj.amount

    # 1. BUSCA PEDIDOS
    orders = db.query(Order).filter(
        Order.store_id == current_user.store_id,
        Order.created_at >= start_time,
        Order.created_at <= end_time,
        not_(Order.status.ilike('%CANCELADO%'))
    ).all()
    
    # Stats
    cat_stats = {"Pizzas": 0, "Esfihas": 0, "Beirutes": 0, "Bebidas": 0, "Outros": 0}
    service_stats = {"Delivery": 0, "Balcao": 0, "Mesa": 0}
    
    # Mapa de Expectativa Financeira
    expected_map = {"Dinheiro": 0.0, "Pix": 0.0, "Cartão": 0.0, "iFood Online": 0.0, "Outros": 0.0}
    
    for o in orders:
        # A. Serviço
        if o.table_number: service_stats['Mesa'] += 1
        elif str(o.delivery_type).lower() == 'balcao' or "retirada" in str(o.address_street or "").lower(): service_stats['Balcao'] += 1
        else: service_stats['Delivery'] += 1

        # B. Produtos
        if o.items_json:
            for item in o.items_json:
                name_upper = str(item.get('title') or item.get('name') or "").upper()
                try: qty = float(item.get('quantity', 1))
                except: qty = 1.0
                
                if 'ESFIHA' in name_upper: cat_stats['Esfihas'] += qty
                elif 'BEIRUTE' in name_upper: cat_stats['Beirutes'] += qty
                elif 'PIZZA' in name_upper: cat_stats['Pizzas'] += qty
                elif any(x in name_upper for x in ['BEBIDA', 'COCA', 'GUARAN', 'SUCO']): cat_stats['Bebidas'] += qty
                else: cat_stats['Outros'] += qty

        # C. Financeiro (Soma vendas por método)
        val = float(o.total_value or 0.0)
        method = (o.payment_method or "").lower()
        
        if "ifood online" in method or "site mercadopago" in method:
            expected_map["iFood Online"] += val
        elif "dinheiro" in method:
            expected_map["Dinheiro"] += val
        elif "pix" in method:
            expected_map["Pix"] += val
        elif any(x in method for x in ["cart", "crédito", "credito", "débito", "debito"]):
            expected_map["Cartão"] += val
        else:
            expected_map["Outros"] += val

    # 2. TRANSAÇÕES (Sangrias/Suplementos)
    ops = db.query(CashTransaction).filter(
        CashTransaction.store_id == current_user.store_id,
        CashTransaction.created_at >= start_time,
        CashTransaction.created_at <= end_time
    ).all()
    
    suplementos = sum(t.amount for t in ops if t.type == 'SUPLEMENTO')
    sangrias = sum(t.amount for t in ops if t.type in ['SANGRIA', 'PAGAMENTO_MOTOBOY']) # Inclui pgto motoboy como saída de dinheiro

    # AJUSTE FINAL DO DINHEIRO ESPERADO
    # Dinheiro = Abertura + Vendas(Dinheiro) + Suplementos - Sangrias
    expected_map["Dinheiro"] = opening_val + expected_map["Dinheiro"] + suplementos - sangrias

    # 3. MOTOBOYS
    driver_txs = db.query(CashTransaction).filter(
        CashTransaction.store_id == current_user.store_id,
        CashTransaction.type.ilike('%PAGAMENTO_MOTOBOY%'),
        CashTransaction.created_at >= start_time,
        CashTransaction.created_at <= end_time
    ).all()
    
    driver_summary = {}
    total_paid_drivers = 0.0
    for tx in driver_txs:
        name = tx.description.replace("Pagamento ", "").replace("Pgto ", "").split('(')[0].strip()
        if name not in driver_summary: driver_summary[name] = 0.0
        driver_summary[name] += tx.amount
        total_paid_drivers += tx.amount

    driver_champion = max(driver_summary, key=driver_summary.get) if driver_summary else None

    return {
        "meta": {
            "id": closing.id,
            "period": f"{start_time.strftime('%d/%m %H:%M')} até {end_time.strftime('%H:%M')}",
            "closer": closing.closer_name,
            "notes": closing.notes,
            "next_opening": closing.next_opening_amount,
            "opening_val": opening_val
        },
        "financial": {
            "system_total": closing.total_system,
            "real_total": closing.total_real,
            "difference": closing.difference,
            "breakdown_real": closing.breakdown_json or {}, # O que foi digitado
            "breakdown_expected": expected_map,             # O que o sistema calculou
            "supply": suplementos,
            "bleed": sangrias
        },
        "sales": {
            "orders_count": len(orders),
            "categories": cat_stats,
            "services": service_stats
        },
        "logistics": {
            "drivers_count": len(driver_summary),
            "total_paid": total_paid_drivers,
            "details": driver_summary,
            "champion": driver_champion
        }
    }


# 2. ABERTURA DE TURNO (IMPORTAR DÍVIDA)
@router.post("/admin/api/finance/driver/{driver_id}/toggle-session")
def admin_toggle_driver_session(
    driver_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role(["owner", "manager"]))
):
    driver = db.query(User).get(driver_id)
    if not driver: return JSONResponse(status_code=404, content={"message": "Erro"})

    # 1. Busca Caixa Aberto
    last_opening = db.query(CashOpening).filter(CashOpening.store_id == current_user.store_id).order_by(desc(CashOpening.created_at)).first()
    last_closing = db.query(CashClosing).filter(CashClosing.store_id == current_user.store_id).order_by(desc(CashClosing.closed_at)).first()
    
    current_cash_id = None
    if last_opening:
        if not last_closing or last_opening.created_at > last_closing.closed_at:
            current_cash_id = last_opening.id

    if not current_cash_id:
        return JSONResponse(status_code=400, content={"message": "Abra o caixa primeiro!"})

    # 2. Toggle Sessão
    active_session = db.query(DriverSession).filter(DriverSession.driver_id == driver_id, DriverSession.end_time == None).first()
    now_utc = datetime.utcnow()

    if active_session:
        # PAUSAR
        active_session.end_time = now_utc
        msg = "Motoboy pausado."
    else:
        # INICIAR
        new_session = DriverSession(
            driver_id=driver_id, 
            store_id=current_user.store_id,
            start_time=now_utc,
            cash_opening_id=current_cash_id
        )
        db.add(new_session)
        db.commit()
        
        # === APLICAÇÃO DA SUA LÓGICA ===
        # Verifica se já existe a Diária neste caixa
        existing_fee = db.query(DriverAdvance).join(DriverSession).filter(
            DriverSession.cash_opening_id == current_cash_id, # Chave do Caixa
            DriverSession.driver_id == driver_id,
            DriverAdvance.reason == 'DIARIA_AUTO'
        ).first()
        
        if not existing_fee:
            fee = driver.driver_fixed_fee or 0.0
            if fee > 0:
                # Cria o registro. 
                # Valor NEGATIVO (-) = Crédito para o motoboy (Casa Deve)
                # Valor POSITIVO (+) = Dívida do motoboy (Vale)
                db.add(DriverAdvance(
                    session_id=new_session.id,
                    amount = -abs(fee), 
                    reason = 'DIARIA_AUTO',
                    is_paid = False # <--- Nasce como Pendente
                ))
                db.commit()

        msg = "Motoboy Ativado."

    return {"success": True, "message": msg}


# 3. FECHAMENTO DE TURNO COM PAGAMENTO (NOVA ROTA)
@router.post("/admin/api/finance/driver/close-day")
def close_driver_day(
    driver_id: int = Form(...),
    payments_json: str = Form(...), 
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role(["owner", "manager"]))
):
    # 1. Contexto
    last_opening = db.query(CashOpening).filter(CashOpening.store_id == current_user.store_id).order_by(desc(CashOpening.created_at)).first()
    
    # Busca a sessão ATIVA para encerrar
    active_session = db.query(DriverSession).filter(DriverSession.driver_id == driver_id, DriverSession.end_time == None).first()
    
    if not active_session: 
        return JSONResponse(status_code=400, content={"message": "Turno já está fechado!"})

    driver = db.query(User).get(driver_id)

    # 2. Recalcula o Total Devido (COM A MESMA LÓGICA DA TELA)
    
    # Busca TODOS os itens pendentes deste caixa
    all_pending = db.query(DriverAdvance).join(DriverSession).filter(
        DriverSession.cash_opening_id == last_opening.id,
        DriverSession.driver_id == driver_id,
        DriverAdvance.is_paid == False
    ).all()
    
    # Filtro Sincronizado
    valid_advances = []
    for item in all_pending:
        if item.session.end_time is not None:
            if "Resto" not in (item.reason or ""):
                item.is_paid = True 
                continue
        valid_advances.append(item)
    
    # Busca pedidos pendentes
    orders = db.query(Order).filter(
        Order.driver_id == driver_id, 
        Order.status.in_(['ENTREGUE', 'CONCLUIDO']),
        Order.cash_opening_id == last_opening.id, 
        Order.is_driver_paid == False,
        Order.created_at >= active_session.start_time
    ).all()
    
    earnings_orders = sum((o.delivery_fee or 0) + (o.driver_tip or 0) for o in orders)
    sum_advances = sum(i.amount for i in valid_advances) 
    
    final_due_system = earnings_orders - sum_advances

    # 3. Pagamentos
    try: payments = json.loads(payments_json)
    except: payments = []
    total_paid_now = sum(float(p['value']) for p in payments)
    
    # 4. O Resto Real
    remaining = final_due_system - total_paid_now
    
    # === AÇÃO DE LIMPEZA ===
    for item in valid_advances: item.is_paid = True
    for order in orders: order.is_driver_paid = True
        
    # Gera o Resto
    if abs(remaining) > 0.01:
        amount_record = -remaining 
        reason_str = "Resto Anterior (Crédito)" if remaining > 0 else "Resto Anterior (Dívida)"
        
        db.add(DriverAdvance(
            session_id=active_session.id,
            amount=amount_record,
            reason=f"{reason_str} - {datetime.now().strftime('%d/%m %H:%M')}",
            is_paid=False 
        ))

    # 5. Logs e Fim
    active_session.end_time = datetime.utcnow()
    
    # === CORREÇÃO AQUI: VÍNCULO COM CAIXA ===
    for p in payments:
        val = float(p['value'])
        if val > 0:
            if p['method'] == 'DINHEIRO':
                # Registra SAÍDA FÍSICA no caixa correto
                db.add(CashTransaction(
                    store_id=current_user.store_id, 
                    user_id=current_user.id, 
                    type='PAGAMENTO_MOTOBOY', 
                    amount=val, 
                    description=f"Pgto {driver.full_name}",
                    cash_opening_id=last_opening.id  # <--- VÍNCULO OBRIGATÓRIO
                ))
            else:
                # Registra apenas log (não afeta gaveta, mas fica no histórico)
                 db.add(CashTransaction(
                     store_id=current_user.store_id, 
                     user_id=current_user.id, 
                     type='PAGAMENTO_MOTOBOY_OUTROS', 
                     amount=val, 
                     description=f"Pgto {driver.full_name} ({p['method']})",
                     cash_opening_id=last_opening.id  # <--- VÍNCULO OBRIGATÓRIO
                 ))

    db.commit()
    return {"success": True, "message": f"Fechado. Restante: R$ {remaining:.2f}"}


@router.post("/admin/api/finance/driver/update-fee")
def update_driver_fee(
    driver_id: int = Form(...),
    new_fee: float = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role(["owner", "manager"]))
):
    driver = db.query(User).get(driver_id)
    if not driver: return JSONResponse(status_code=404, content={"message": "Motoboy não encontrado"})
    
    driver.driver_fixed_fee = new_fee
    db.commit()
    return {"success": True}


# --- ROTA DE PAGAMENTO DE DÍVIDA (CORRIGIDA) ---
@router.post("/admin/api/customer/pay-debt")
async def pay_customer_debt(  # <--- async É OBRIGATÓRIO AQUI
    order_ids: str = Form(...),
    payment_method: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role(["owner", "manager"]))
):
    try:
        ids_list = [int(id) for id in order_ids.split(',')]
        orders = db.query(Order).filter(
            Order.store_id == current_user.store_id,
            Order.id.in_(ids_list)
        ).all()
        
        if not orders:
            return JSONResponse(status_code=404, content={"message": "Pedidos não encontrados"})

        total_paid = 0.0
        customer_name = orders[0].customer_name if orders else "Cliente"
        now_str = datetime.now().strftime("%d/%m %H:%M")
        
        for o in orders:
            total_paid += (o.total_value or 0.0)
            # Marca como pago e remove a tag 'Fiado' para sair da lista
            o.payment_method = f"Pago em {now_str} ({payment_method})"
            o.status = "CONCLUIDO" # Finaliza o pedido
            
        # Lança no Caixa do Dia (Entrada de dinheiro hoje referente a venda antiga)
        trans_type = "RECEBIMENTO_FIADO_OUTROS"
        if "dinheiro" in payment_method.lower(): trans_type = "RECEBIMENTO_FIADO_DINHEIRO"
        elif "pix" in payment_method.lower(): trans_type = "RECEBIMENTO_FIADO_PIX"
        elif "cart" in payment_method.lower(): trans_type = "RECEBIMENTO_FIADO_CARTAO"
        
        transaction = CashTransaction(
            store_id=current_user.store_id,
            user_id=current_user.id,
            type=trans_type,
            amount=total_paid,
            description=f"Rec. Fiado {customer_name} ({len(orders)} peds)"
        )
        db.add(transaction)
        db.commit()
        
        # Avisa o frontend para atualizar o caixa (CORREÇÃO DO ERRO)
        await manager.broadcast(current_user.store_id, "cash_update")

        return {"success": True, "message": f"Recebido R$ {total_paid:.2f}"}

    except Exception as e:
        db.rollback()
        print(f"❌ Erro Pagamento Fiado: {e}")
        # Imprime o traceback completo no log para ajudar a debugar se persistir
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"message": str(e)})
    


# ==========================================
#      NOVA GESTÃO FINANCEIRA DE EQUIPE
# ==========================================


# 1. PEDIDO COM CONTA FUNCIONÁRIO (GARANTIA DE NÃO DUPLICAR)
@router.post("/admin/api/finance/pay-order-employee")
async def pay_order_via_employee(
    order_id: int = Form(...),
    employee_id: int = Form(...),
    discount_percent: float = Form(0.0),
    final_value: Optional[float] = Form(None),
    admin_password: Optional[str] = Form(None), 
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    # 1. Validação de Permissão
    authorizer = current_user
    if current_user.role not in ['owner', 'manager', 'ceo']:
        if not admin_password: return JSONResponse(status_code=403, content={"message": "Senha obrigatória."})
        # (Lógica de verificar senha admin se necessário)
        pass 

    order = db.query(Order).get(order_id)
    employee = db.query(User).get(employee_id)
    if not order or not employee: return JSONResponse(status_code=404, content={"message": "Erro dados"})

    # 2. Cálculo do Valor Final
    original_total = order.total_value or 0.0
    
    if final_value is not None:
        final_amount = float(final_value)
        discount_val = original_total - final_amount
    else:
        discount_val = 0.0
        if discount_percent > 0:
            discount_val = original_total * (discount_percent / 100)
        final_amount = original_total - discount_val

    # Identificador Único
    reason_text = f"Consumo Ped #{order.wabiz_id or order.id}"

    # ======================================================================
    # 🛡️ LÓGICA DE PROTEÇÃO CONTRA DUPLICIDADE (UPSERT)
    # ======================================================================
    
    # Verifica se já existe transação deste pedido para este funcionário
    existing_tx = db.query(EmployeeTransaction).filter(
        EmployeeTransaction.order_id == order.id,
        EmployeeTransaction.employee_id == employee.id,
        EmployeeTransaction.transaction_type == TransactionType.consumo
    ).first()

    if existing_tx:
        # --- MODO ATUALIZAÇÃO (Corrige o valor anterior) ---
        print(f"🔄 [Financeiro] Atualizando consumo existente Pedido #{order.id}")
        
        # 1. Recupera quanto foi cobrado antes (para estornar matematicamente)
        # O valor no banco é negativo (-6.00), pegamos absoluto (6.00)
        old_debit = abs(existing_tx.amount)
        
        # 2. Atualiza o registro histórico
        existing_tx.amount = -abs(final_amount) # Novo valor
        existing_tx.updated_at = datetime.utcnow()
        existing_tx.description = reason_text # Atualiza texto caso tenha mudado ID
        
        # 3. Atualiza o Saldo Real
        if employee.role == 'driver':
            # MOTOBOY: Busca o 'Vale' (DriverAdvance) correspondente na sessão aberta
            # Como DriverAdvance não tem order_id, buscamos pelo texto do motivo
            advance = db.query(DriverAdvance).join(DriverSession).filter(
                DriverSession.driver_id == employee.id,
                DriverSession.end_time == None, # Sessão ativa
                DriverAdvance.reason == reason_text
            ).first()
            
            if advance:
                # Apenas atualiza o valor da dívida existente
                advance.amount = final_amount
            else:
                # Se não achou (ex: apagaram na mão), cria um novo
                # Mas isso é raro com a trava acima
                session = db.query(DriverSession).filter(DriverSession.driver_id == employee.id, DriverSession.end_time == None).first()
                if session:
                    db.add(DriverAdvance(session_id=session.id, amount=final_amount, reason=reason_text))
        
        else:
            # STAFF: Carteira Permanente
            # Estorna o velho e cobra o novo
            # Ex: Saldo era 100. Cobrou 6 (ficou 94). Agora é 40.
            # Novo Saldo = 94 + 6 - 40 = 60.
            current_bal = employee.driver_balance or 0.0
            employee.driver_balance = current_bal + old_debit - abs(final_amount)

    else:
        # --- MODO CRIAÇÃO (Primeira vez) ---
        
        # 1. Histórico
        transaction = EmployeeTransaction(
            employee_id=employee.id,
            admin_id=authorizer.id,
            order_id=order.id,
            amount= -abs(final_amount),
            transaction_type=TransactionType.consumo,
            description=reason_text,
            discount_percentage=0 # Não usamos mais percentual
        )
        db.add(transaction)

        # 2. Saldo Real
        if employee.role == 'driver':
            # Garante sessão
            session = db.query(DriverSession).filter(DriverSession.driver_id == employee.id, DriverSession.end_time == None).first()
            if not session:
                tz_br = pytz.timezone('America/Sao_Paulo')
                session = DriverSession(driver_id=employee.id, store_id=current_user.store_id, start_time=datetime.now(tz_br))
                db.add(session)
                db.commit()

            # Cria Vale
            advance = DriverAdvance(
                session_id=session.id,
                amount=final_amount,
                reason=reason_text
            )
            db.add(advance)
        else:
            # Desconta Carteira
            current_bal = employee.driver_balance or 0.0
            employee.driver_balance = current_bal - abs(final_amount)

    # ======================================================================

    # Finaliza Pedido
    order.payment_method = f"Conta Func. ({employee.full_name})"
    order.discount = discount_val
    order.total_value = final_amount
    order.status = "CONCLUIDO"
    
    db.commit()
    await manager.broadcast(current_user.store_id, "update")
    
    return {"success": True, "message": f"Atualizado: R$ {final_amount:.2f} para {employee.full_name}"}


# 2. CORREÇÃO: Caminho completo na rota
@router.get("/admin/api/finance/ledger/advanced")
def get_advanced_ledger(
    employee_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    query = db.query(EmployeeTransaction).join(User, EmployeeTransaction.employee_id == User.id).filter(
        User.store_id == current_user.store_id
    )
    
    if employee_id:
        query = query.filter(EmployeeTransaction.employee_id == employee_id)
    
    if start_date:
        dt_start = datetime.strptime(start_date, '%Y-%m-%d')
        query = query.filter(EmployeeTransaction.created_at >= dt_start)
        
    if end_date:
        dt_end = datetime.strptime(end_date, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
        query = query.filter(EmployeeTransaction.created_at <= dt_end)

    transactions = query.order_by(desc(EmployeeTransaction.created_at)).limit(200).all()
    
    data = []
    total_debit = 0.0
    total_credit = 0.0
    
    # --- CONFIGURAÇÃO DE FUSO ---
    tz_br = pytz.timezone('America/Sao_Paulo')
    utc = pytz.utc
    # ----------------------------

    for t in transactions:
        emp = db.query(User).get(t.employee_id)
        adm = db.query(User).get(t.admin_id)
        
        # --- CONVERSÃO UTC -> BR ---
        date_str = "--/-- --:--"
        if t.created_at:
            # Garante que o Python sabe que a data do banco é UTC
            raw_dt = t.created_at.replace(tzinfo=utc) 
            # Converte para SP
            local_dt = raw_dt.astimezone(tz_br)
            date_str = local_dt.strftime('%d/%m/%Y %H:%M')
        # ---------------------------

        data.append({
            "id": t.id,
            "date": date_str, # <--- Usa a data corrigida
            "employee_name": emp.full_name if emp else "Desconhecido",
            "employee_full_name": emp.full_name if emp else "Desconhecido",
            "type": t.transaction_type.value,
            "description": t.description,
            "amount": t.amount,
            "admin": adm.full_name if adm else "Sistema",
            "order_id": t.order_id
        })
        
        if t.amount < 0: total_debit += t.amount
        else: total_credit += t.amount

    return {
        "transactions": data,
        "summary": {
            "credits": total_credit,
            "debits": total_debit,
            "balance_period": total_credit + total_debit
        }
    }


# 3. LANÇAMENTO MANUAL (VALE / BÔNUS / PAGAMENTO)
@router.post("/admin/api/finance/transaction/manual")
def create_manual_transaction(
    employee_id: int = Form(...),
    type: str = Form(...), # 'vale', 'bonus', 'pagamento'
    amount: float = Form(...),
    description: str = Form(...),
    payment_method: str = Form("DINHEIRO"), # <--- NOVO CAMPO (Padrão Dinheiro)
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role(["owner", "manager", "ceo"]))
):
    employee = db.query(User).get(employee_id)
    if not employee: return JSONResponse(status_code=404, content={"message": "Erro user"})

    final_amount = abs(amount)
    
    # Define o Tipo para o Extrato do Funcionário (Isso não muda)
    enum_type = TransactionType.vale
    if type == 'bonus': enum_type = TransactionType.bonus
    elif type == 'pagamento': enum_type = TransactionType.pagamento

    # Define o Sinal (Débito ou Crédito na carteira do funcionário)
    balance_change = -final_amount if type == 'vale' else final_amount

    # 1. REGISTRO NO HISTÓRICO PERMANENTE (Audit)
    tx = EmployeeTransaction(
        employee_id=employee.id,
        admin_id=current_user.id,
        amount=balance_change,
        transaction_type=enum_type,
        description=description
    )
    db.add(tx)

    # Identifica Caixa Aberto
    last_opening = db.query(CashOpening).filter(CashOpening.store_id == current_user.store_id).order_by(desc(CashOpening.created_at)).first()
    last_closing = db.query(CashClosing).filter(CashClosing.store_id == current_user.store_id).order_by(desc(CashClosing.closed_at)).first()
    
    current_cash_id = None
    if last_opening:
        if not last_closing or last_opening.created_at > last_closing.closed_at:
            current_cash_id = last_opening.id

    # 2. LÓGICA MOTOBOY VS STAFF
    if employee.role == 'driver':
        session = db.query(DriverSession).filter(DriverSession.driver_id == employee.id, DriverSession.end_time == None).first()
        if not session:
            tz_br = pytz.timezone('America/Sao_Paulo')
            session = DriverSession(driver_id=employee.id, store_id=current_user.store_id, start_time=datetime.now(tz_br), cash_opening_id=current_cash_id)
            db.add(session)
            db.commit()

        advance_val = final_amount if type == 'vale' else -final_amount
        if type == 'pagamento': advance_val = final_amount

        advance = DriverAdvance(
            session_id=session.id,
            amount=advance_val,
            reason=f"{type.capitalize()}: {description}"
        )
        db.add(advance)
    else:
        current = employee.driver_balance or 0.0
        employee.driver_balance = current + balance_change

    # 3. MOVIMENTAÇÃO DE CAIXA (AQUI ESTÁ A MUDANÇA)
    # Se for Vale ou Pagamento, gera saída no caixa
    if type == 'vale' or type == 'pagamento':
        
        # Decide o tipo de transação no caixa
        trans_type = 'SANGRIA' # Padrão Dinheiro (Afeta a gaveta)
        
        if payment_method == 'PIX':
            trans_type = 'SANGRIA_PIX' # Novo tipo (Não afeta a gaveta de dinheiro)
        
        cash_op = CashTransaction(
            store_id=current_user.store_id,
            user_id=current_user.id,
            type=trans_type, 
            amount=final_amount, 
            description=f"{type.capitalize()} para {employee.full_name} ({payment_method})",
            cash_opening_id=current_cash_id
        )
        db.add(cash_op)

    db.commit()
    
    return {"success": True, "new_balance": employee.driver_balance}


# 4. CORREÇÃO: Caminho completo na rota (Esta que estava dando 404)
@router.get("/admin/api/finance/team/balances")
def get_team_balances(
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    users = db.query(User).filter(
        User.store_id == current_user.store_id,
        User.role != 'client'
    ).all()
    
    data = []
    for u in users:
        data.append({
            "id": u.id,
            "name": u.full_name,
            "role": u.role,
            "balance": u.driver_balance or 0.0 
        })
        
    return sorted(data, key=lambda x: x['balance'], reverse=False)
