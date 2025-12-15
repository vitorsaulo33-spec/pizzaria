from fastapi import APIRouter, Request, Depends, Form, Query, WebSocket, WebSocketDisconnect, status, BackgroundTasks # <--- CONFIRME SE ESTÁ AQUI
from fastapi.security.utils import get_authorization_scheme_param
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import desc, or_, and_, cast, String, not_, func
from fastapi.responses import HTMLResponse, JSONResponse
from datetime import datetime, timedelta
import pytz
import json
from typing import Optional
from services.background_jobs import get_active_adapters
from services.integrations.ifood import IfoodAdapter, process_ifood_update
from services.integrations.wabiz import WabizAdapter, process_wabiz_update
import unicodedata
import re
# Imports Locais
from database import get_db
# Adicione CashOpening na lista de imports de models
from models import Order, User, Product, DeliveryFee, Customer, Address, DriverSession, CashOpening, CashClosing, ProductMapping
from dependencies import templates, check_db_auth, get_today_stats, get_mixed_current_user, get_current_waiter
from services.whatsapp import notify_pickup_ready
from services.stock_engine import return_stock_from_order, enrich_order_with_combo_data
from services.utils import normalize_phone, recover_historical_ip, upsert_customer_smart, dispatch_smart_event, get_active_cash_id
from services.normalizer import normalize_order_items_for_view

from services.sockets import manager
import asyncio

from services.sockets import manager
from auth import ALGORITHM, SECRET_KEY, verify_password, create_access_token
from jose import jwt, JWTError
import asyncio
import base64
from services.utils import get_br_time # <--- Importante

router = APIRouter()


# ==========================================
#        GESTÃO DE PEDIDOS (PDV & KANBAN)
# ==========================================


@router.get("/admin/orders", response_class=HTMLResponse)
def admin_orders_view(request: Request, db: Session = Depends(get_db), current_user: User = Depends(check_db_auth), start: Optional[str] = None, end: Optional[str] = None, search: Optional[str] = None):
    # 1. Filtros de Data
    tz_br = pytz.timezone('America/Sao_Paulo')
    now_br = datetime.now(tz_br)
    
    if start: start_dt = datetime.strptime(start, '%Y-%m-%d')
    else: start_dt = now_br.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
        
    if end: end_dt = datetime.strptime(end, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
    else: end_dt = now_br.replace(hour=23, minute=59, second=59).replace(tzinfo=None)

    # 2. Busca Pedidos
    query = db.query(Order).filter(Order.store_id == current_user.store_id, Order.created_at >= start_dt, Order.created_at <= end_dt)

    if search:
        term = f"%{search}%"
        query = query.filter(or_(Order.customer_name.ilike(term), Order.customer_phone.ilike(term), Order.wabiz_id.ilike(term)))

    orders_db = query.order_by(desc(Order.created_at)).all()

    # 3. SERIALIZAÇÃO MANUAL (CORREÇÃO DO ERRO E PEDIDOS WABIZ)
    orders_clean = []
    for o in orders_db:
        # Garante que status vazio vire PENDENTE (Correção Wabiz)
        status_safe = str(o.status) if o.status else "PENDENTE"
        if status_safe == "None": status_safe = "PENDENTE"

        orders_clean.append({
            "id": int(o.id),
            "wabiz_id": str(o.wabiz_id) if o.wabiz_id else "MANUAL",
            "customer_name": str(o.customer_name) if o.customer_name else "Cliente",
            "customer_phone": str(o.customer_phone) if o.customer_phone else "",
            "address_street": str(o.address_street) if o.address_street else "",
            "address_number": str(o.address_number) if o.address_number else "",
            "address_neighborhood": str(o.address_neighborhood) if o.address_neighborhood else "",
            "address_city": str(o.address_city) if o.address_city else "",
            "payment_method": str(o.payment_method) if o.payment_method else "",
            "total_value": float(o.total_value) if o.total_value else 0.0,
            "status": status_safe, 
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "items_json": o.items_json if o.items_json else []
        })

    # 4. Dados Auxiliares
    products = db.query(Product).filter(Product.store_id == current_user.store_id, Product.is_active == True).all()
    products_json = [{"id": p.id, "name": p.name, "price": float(p.price), "description": p.description or ""} for p in products]
    
    # Alteração para buscar apenas motoboys com sessão ATIVA (end_time == None)
    
    drivers = db.query(User).join(DriverSession, User.id == DriverSession.driver_id)\
        .filter(
            User.store_id == current_user.store_id, 
            User.role == 'driver',
            DriverSession.end_time == None
        ).all()
    
    # Carrega Taxas (Try/Except caso a tabela não exista ainda)
    fees_json = []
    try:
        fees = db.query(DeliveryFee).filter(DeliveryFee.store_id == current_user.store_id).all()
        fees_json = [{"neighborhood": f.neighborhood, "fee": f.fee} for f in fees]
    except: pass

    response = templates.TemplateResponse("orders.html", {
        "request": request, "current_user": current_user, 
        "orders": orders_clean, "products_json": products_json, 
        "drivers": drivers, "fees_json": fees_json,
        "filters": {"start": start_dt.strftime('%Y-%m-%d'), "end": end_dt.strftime('%Y-%m-%d'), "search": search or ""}
    })

    # 2. CORREÇÃO: Injeta o Cookie para o WebSocket
    token = create_access_token(data={"sub": current_user.email})

    return templates.TemplateResponse("orders.html", {
        "request": request, 
        "current_user": current_user, 
        "orders": orders_clean, 
        "products_json": products_json, 
        "drivers": drivers, 
        "fees_json": fees_json,
        "filters": {"start": start_dt.strftime('%Y-%m-%d'), "end": end_dt.strftime('%Y-%m-%d'), "search": search or ""},
        "access_token": token # <--- Enviando token
    })
    
    
@router.get("/admin/api/orders/list")
def get_orders_api(
    request: Request,
    tab: str = Query(..., description="delivery, balcao, mesas"),
    search: Optional[str] = None,
    platform: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    driver_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_mixed_current_user)
):
    try:
        # 1. IDENTIFICAÇÃO DO CAIXA
        tz_br = pytz.timezone('America/Sao_Paulo')
        now = datetime.now(tz_br).replace(tzinfo=None)
        
        last_opening = db.query(CashOpening).filter(CashOpening.store_id == current_user.store_id).order_by(desc(CashOpening.created_at)).first()
        last_closing = db.query(CashClosing).filter(CashClosing.store_id == current_user.store_id).order_by(desc(CashClosing.closed_at)).first()

        is_box_open = False
        current_cash_id = None
        start_dt_box = now 

        if last_opening:
            if last_closing and last_closing.closed_at > last_opening.created_at:
                is_box_open = False 
            else:
                is_box_open = True
                current_cash_id = last_opening.id
                start_dt_box = last_opening.created_at
        
        if not is_box_open:
             start_dt_box = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # --- CORREÇÃO AQUI: Cria a query base ---
        all_box_orders = []

        if is_box_open:
            stats_query = db.query(Order).filter(Order.store_id == current_user.store_id)
            
            if current_cash_id:
                stats_query = stats_query.filter(
                    or_(
                        Order.cash_opening_id == current_cash_id,
                        # Pega também orfãos recentes para a função de "varrer" funcionar visualmente assim que abre
                        and_(Order.cash_opening_id == None, Order.created_at >= start_dt_box)
                    )
                )
            else:
                # Fallback de segurança
                stats_query = stats_query.filter(Order.created_at >= start_dt_box)

            # Só executa a query pesada se o caixa estiver aberto
            all_box_orders = stats_query.with_entities(
                Order.id,
                Order.items_json,
                Order.payment_method,
                Order.wabiz_id,
                Order.status,
                Order.table_number,
                Order.address_street
            ).all()


        prod_stats = {"pizzas": 0, "esfihas": 0, "beirutes": 0}
        platform_stats = {"ifood": 0, "wabiz": 0, "pdv": 0}
        counts = {"delivery": {"total": 0, "prep": 0, "ready": 0}, "balcao": {"total": 0, "prep": 0, "ready": 0}, "mesas": 0}

        # === 🧠 SUPER MAPA DE CATEGORIAS (ID + NOME + EXTERNAL CODE) ===
        # 1. Carrega Produtos Normais
        products_db = db.query(Product).options(joinedload(Product.category)).filter(Product.store_id == current_user.store_id).all()
        
        # 2. Carrega Mapeamentos (Integrações)
        mappings_db = db.query(ProductMapping).options(joinedload(ProductMapping.product).joinedload(Product.category)).filter(ProductMapping.store_id == current_user.store_id).all()
        
        cat_map = {} # Chave -> Nome da Categoria (UPPER)

        # Indexa Produtos por ID e Nome
        for p in products_db:
            if p.category:
                c_name = p.category.name.upper()
                cat_map[str(p.id)] = c_name
                cat_map[p.name.strip().upper()] = c_name

        # Indexa por Código Externo (O Pulo do Gato para iFood/Wabiz)
        for m in mappings_db:
            if m.product and m.product.category and m.external_code:
                c_name = m.product.category.name.upper()
                cat_map[str(m.external_code)] = c_name

        # Loop de Contagem (LÓGICA CORRIGIDA)
        for o in all_box_orders:
            
            # Pula cancelados
            if o.status and "CANCELADO" in o.status.upper(): 
                continue
            
            # A. Produtos
            if o.items_json:
                for item in o.items_json:
                    try: qty = float(item.get('quantity', 1))
                    except: qty = 1.0
                    
                    # Ignora itens que são apenas cabeçalhos de combo ou taxas
                    name_upper = str(item.get('title') or item.get('name') or "").upper()
                    if "TAXA" in name_upper or "ENTREGA" in name_upper: continue

                    # 1. Tenta identificar categoria pelo mapa (Código/ID)
                    possible_keys = [
                        str(item.get('externalCode', '')),
                        str(item.get('external_code', '')),
                        str(item.get('code', '')),
                        str(item.get('id', '')),
                        str(item.get('product_id', '')),
                    ]
                    
                    found_cat = ""
                    for key in possible_keys:
                        if key and key in cat_map:
                            found_cat = cat_map[key]
                            break
                    
                    # 2. DECISÃO HÍBRIDA (Prioriza Nome > Categoria)
                    # Isso corrige "Esfiha cadastrada na categoria Pizza"
                    
                    # Verifica ESFIHA (Prioridade Alta)
                    if 'ESFIHA' in name_upper or 'ESFIHA' in found_cat:
                        prod_stats['esfihas'] += int(qty)
                        
                    # Verifica BEIRUTE
                    elif 'BEIRUTE' in name_upper or 'BEIRUTE' in found_cat:
                        prod_stats['beirutes'] += int(qty)
                        
                    # Verifica PIZZA (Última prioridade, pois é termo genérico)
                    elif 'PIZZA' in name_upper or 'PIZZA' in found_cat:
                        # Evita contar "Massa de Pizza" ou "Borda de Pizza" como uma Pizza inteira
                        if "BORDA" not in name_upper and "MASSA" not in name_upper:
                            prod_stats['pizzas'] += int(qty)
                            

            # B. Plataforma
            pm = (o.payment_method or "").lower()
            wid = str(o.wabiz_id or "")
            if "ifood" in pm: platform_stats["ifood"] += 1
            elif wid and not wid.startswith("M-"): platform_stats["wabiz"] += 1
            else: platform_stats["pdv"] += 1

            # C. Badges
            st = (o.status or "").upper()
            if o.table_number:
                if st not in ['ENTREGUE', 'CONCLUIDO']: counts['mesas'] += 1
            else:
                is_balcao = "RETIRADA" in (o.address_street or "").upper() or not o.address_street
                target = counts['balcao'] if is_balcao else counts['delivery']
                if st not in ['CONCLUIDO']:
                    target['total'] += 1
                    if st in ['PREPARO', 'PENDENTE', 'FORNO']: target['prep'] += 1
                    if st in ['PRONTO', 'SAIU_ENTREGA']: target['ready'] += 1

        # 3. QUERY OPERACIONAL (LISTA VISUAL)
        list_query = db.query(Order).options(joinedload(Order.driver)).filter(Order.store_id == current_user.store_id)

        if start:
            s_dt = datetime.strptime(start, '%Y-%m-%d')
            e_dt = datetime.strptime(end, '%Y-%m-%d').replace(hour=23, minute=59, second=59) if end else now
            list_query = list_query.filter(Order.created_at >= s_dt, Order.created_at <= e_dt)
        else:
            active_statuses = ['PENDENTE', 'PREPARO', 'PRONTO', 'SAIU_ENTREGA', 'FORNO', 'EXPEDICAO']
            if is_box_open:
                if current_cash_id and hasattr(Order, 'cash_opening_id'):
                     list_query = list_query.filter(or_(Order.cash_opening_id == current_cash_id, Order.created_at >= start_dt_box, Order.status.in_(active_statuses)))
                else:
                    list_query = list_query.filter(or_(Order.created_at >= start_dt_box, Order.status.in_(active_statuses)))
            else:
                last_closing_dt = last_closing.closed_at if last_closing else now
                list_query = list_query.filter(or_(Order.created_at >= last_closing_dt, Order.status.in_(active_statuses)))

        if search and search.strip():
            term = f"%{search}%"
            list_query = list_query.filter(or_(Order.customer_name.ilike(term), Order.wabiz_id.ilike(term), cast(Order.id, String).ilike(term)))

        if platform and platform != 'all':
            if platform == 'ifood': list_query = list_query.filter(Order.payment_method.ilike('%ifood%'))
            elif platform == 'wabiz': list_query = list_query.filter(and_(not_(Order.wabiz_id.ilike('M-%')), not_(Order.payment_method.ilike('%ifood%'))))
            elif platform == 'pdv': list_query = list_query.filter(or_(Order.wabiz_id.ilike('M-%'), Order.payment_method.ilike('%pdv%')))

        
        if driver_id:
            list_query = list_query.filter(Order.driver_id == driver_id)
        
        if tab == 'delivery':
            list_query = list_query.filter(Order.table_number == None, Order.address_street != None, Order.address_street != '', not_(Order.address_street.ilike('Retirada%')), not_(Order.address_street.ilike('%Balcão%')), not_(Order.address_street.ilike('%Balcao%')))
        elif tab == 'balcao':
            list_query = list_query.filter(Order.table_number == None, or_(Order.address_street.ilike('Retirada%'), Order.address_street.ilike('%Balcão%'), Order.address_street.ilike('%Balcao%'), Order.address_street == None, Order.address_street == ''))
        elif tab == 'mesas':
            list_query = list_query.filter(Order.table_number != None, not_(Order.status.in_(['ENTREGUE', 'CONCLUIDO', 'FINALIZADO'])), not_(Order.status.ilike('CANCELADO%')))

        orders_list = list_query.order_by(desc(Order.created_at)).all()
        
        # 4. Montagem
        data = []
        for o in orders_list:
            if o.status and "CANCELADO" in o.status.upper(): continue
            
            dtype = o.delivery_type 
            
            if dtype == 'delivery' and "retirada" in (o.address_street or "").lower():
                dtype = "balcao"

            if not dtype:
                dtype = "delivery" 
                if o.table_number: dtype = "mesa"
                elif "retirada" in (o.address_street or "").lower(): dtype = "balcao"
                
            
            driver_name = o.driver.full_name if o.driver else "Sem Motoboy"
            neighborhood = o.address_neighborhood if o.address_neighborhood else "OUTROS"
            final_delivery_fee = float(o.delivery_fee or 0.0)
            
            if final_delivery_fee == 0 and o.items_json:
                 for item in o.items_json:
                    try:
                        title = (item.get('title') or item.get('name') or "").lower()
                        price = float(item.get('price', 0)) * float(item.get('quantity', 1))
                        if ('taxa' in title or 'entrega' in title) and price > 0: final_delivery_fee += price
                    except: pass

            # --- CÁLCULO DE TEMPO CORRIGIDO (UTC -> BR) ---
            utc = pytz.utc
            br_zone = pytz.timezone('America/Sao_Paulo')
            now_aware = datetime.now(br_zone)
            
            minutes_elapsed = 0
            formatted_time = "--:--"
            
            if o.created_at:
                raw_dt = o.created_at
                # Se vier sem fuso do banco, marca como UTC
                if raw_dt.tzinfo is None:
                    raw_dt = utc.localize(raw_dt)
                
                # Converte para Brasil
                local_dt = raw_dt.astimezone(br_zone)
                
                formatted_time = local_dt.strftime('%H:%M')
                minutes_elapsed = int((now_aware - local_dt).total_seconds() / 60)
            # ---------------------------------------------
            
            pm = (o.payment_method or "").lower()
            wid = str(o.wabiz_id or "")
            plat_code = "PDV"
            if "ifood" in pm: plat_code = "IFOOD"
            elif not wid.startswith("M-"): plat_code = "WABIZ"

            items_summary = ""
            if o.items_json:
                items_summary = ", ".join([f"{int(i.get('quantity',1))}x {i.get('title') or i.get('name') or 'Item'}" for i in o.items_json])



            final_name = o.customer_name or "Cliente"
            
            # --- SUBSTIUIÇÃO: USANDO O NORMALIZADOR UNIFICADO ---
            try:
                items_normalized = normalize_order_items_for_view(o)
            except Exception as e:
                print(f"⚠️ Erro ao normalizar pedido {o.id}: {e}")
                # Fallback: Usa os itens brutos se der erro, para não travar a tela
                items_normalized = o.items_json or [] 
            
            # Gera uma string simples para resumo
            try:
                items_summary = ", ".join([f"{i.get('quantity', 1)}x {i.get('name', 'Item')}" for i in items_normalized])
            except:
                items_summary = "Erro ao listar itens"
            

            data.append({
                "id": o.id,
                "wabiz_id": o.wabiz_id or str(o.id),
                "name": final_name,
                "customer_name": final_name,
                "phone": o.customer_phone,
                "customer_phone": o.customer_phone,
                "total": float(o.total_value or 0.0),
                "discount": float(o.discount or 0.0),
                "delivery_fee": final_delivery_fee,
                "service_fee": float(o.service_fee or 0.0),
                "status": o.status,
                "payment": o.payment_method,
                "time": formatted_time,
                "minutes_elapsed": minutes_elapsed,
                "items": items_normalized,
                "items_desc": items_summary,
                
                # --- CAMPOS ADICIONADOS PARA CORRIGIR O BUG ---
                "address_street": o.address_street,
                "address_number": o.address_number,
                # ----------------------------------------------
                
                "address": f"{o.address_street}, {o.address_number} - {neighborhood}" if o.address_street else "Retirada",
                "neighborhood": neighborhood,
                "table": o.table_number,
                "delivery_type": dtype,
                "driver_name": driver_name,
                "driver_id": o.driver_id,
                "driver_tip": float(o.driver_tip or 0.0),
                "customer_credit": float(o.customer_credit or 0.0),
                "platform": plat_code,
                
                "obs": o.notes or "",
                
            })

        def normalize_neigh_py(text):
            if not text: return "OUTROS"
            text = unicodedata.normalize('NFKD', str(text)).encode('ASCII', 'ignore').decode('ASCII')
            return "".join([c for c in text if c.isalnum() or c.isspace()]).strip().upper()

        kanban_data = { "PREPARO": [], "PRONTO": [], "SAIU_ENTREGA": [], "ENTREGUE": [] }
        pronto_groups = {} 

        for item in data:
            raw_st = (item['status'] or '').upper()
            st = 'PREPARO' if raw_st == 'PENDENTE' else raw_st
            if raw_st in ['FORNO', 'EXPEDICAO', 'PRONTO_COZINHA']:
                st = 'PREPARO'
                item['css_class'] = 'card-oven'

            if st == 'PREPARO':
                kanban_data['PREPARO'].append(item)
            elif st == 'SAIU_ENTREGA':
                kanban_data['SAIU_ENTREGA'].append(item)
            elif st == 'ENTREGUE':
                kanban_data['ENTREGUE'].append(item)
            elif st == 'PRONTO':
                neigh_raw = item.get('neighborhood') or 'Outros'
                neigh_key = normalize_neigh_py(neigh_raw)
                if neigh_key not in pronto_groups:
                    pronto_groups[neigh_key] = {"name": neigh_raw.upper().strip(), "orders": [], "total_value": 0.0}
                pronto_groups[neigh_key]['orders'].append(item)
                pronto_groups[neigh_key]['total_value'] += item['total']
            elif st in kanban_data:
                kanban_data[st].append(item)

        kanban_data['PRONTO'] = sorted(list(pronto_groups.values()), key=lambda x: len(x['orders']), reverse=True)
        if len(kanban_data['ENTREGUE']) > 20:
             kanban_data['ENTREGUE'] = kanban_data['ENTREGUE'][-20:]
             
        # Sobrescreve a contagem do loop para garantir que só pegue mesas não finalizadas
        real_open_tables = db.query(Order).filter(
            Order.store_id == current_user.store_id,
            Order.table_number != None,
            not_(Order.status.in_(['ENTREGUE', 'CONCLUIDO', 'FINALIZADO'])),
            not_(Order.status.ilike('%CANCELADO%'))
        ).count()
        
        counts['mesas'] = real_open_tables
        

        final_orders_list = []
        if tab != 'delivery':
            final_orders_list = data

        return {
            "orders": final_orders_list,
            "kanban": kanban_data, 
            "counts": counts, 
            "product_stats": prod_stats, 
            "platform_stats": platform_stats
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"ERRO API ORDERS: {e}") 
        return JSONResponse(status_code=500, content={"message": str(e)})
    

# --- NOVA ROTA: AUDITORIA DE PEDIDOS ---
@router.get("/admin/api/orders/audit")
def get_audit_orders(
    page: int = 1,
    limit: int = 20,
    search: Optional[str] = None,
    status: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    payment_type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth) # Ou check_role(['owner', 'manager']) se preferir restrito
):
    # 1. Base Query
    query = db.query(Order).options(joinedload(Order.driver), joinedload(Order.store)).filter(Order.store_id == current_user.store_id)

    # 2. Filtros Dinâmicos
    if start_date:
        dt_start = datetime.strptime(start_date, '%Y-%m-%d')
        query = query.filter(Order.created_at >= dt_start)
    
    if end_date:
        dt_end = datetime.strptime(end_date, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
        query = query.filter(Order.created_at <= dt_end)

    if status and status != 'all':
        if status == 'CANCELADO':
            query = query.filter(Order.status.ilike('%CANCELADO%'))
        elif status == 'ATIVOS':
            # Tudo que não é finalizado/cancelado
            query = query.filter(not_(Order.status.in_(['CONCLUIDO', 'ENTREGUE', 'CANCELADO'])))
        else:
            query = query.filter(Order.status == status)

    if payment_type and payment_type != 'all':
        query = query.filter(Order.payment_method.ilike(f"%{payment_type}%"))

    if search:
        term = f"%{search}%"
        query = query.filter(or_(
            Order.customer_name.ilike(term),
            Order.customer_phone.ilike(term),
            Order.wabiz_id.ilike(term),
            cast(Order.id, String).ilike(term)
        ))

    # 3. CÁLCULO DE TOTAIS (No Banco de Dados - Rápido e Preciso)
    # Isso garante que o valor total bata com o filtro, independente da página atual
    summary = query.with_entities(
        func.count(Order.id),
        func.sum(Order.total_value)
    ).first()
    
    total_count = summary[0] or 0
    total_value = summary[1] or 0.0

    # 4. Paginação (Apenas os dados visíveis)
    orders_db = query.order_by(desc(Order.created_at)).offset((page-1)*limit).limit(limit).all()

    # 5. Serialização
    data = []
    for o in orders_db:
        # Formata status bonito
        st_class = "text-slate-400"
        if o.status == 'CONCLUIDO': st_class = "text-emerald-400 font-bold"
        elif 'CANCELADO' in (o.status or ''): st_class = "text-red-400 line-through"
        elif o.status in ['PREPARO', 'FORNO', 'PRONTO']: st_class = "text-blue-400 font-bold"

        data.append({
            "id": o.id,
            "wabiz_id": o.wabiz_id or str(o.id),
            "created_at": o.created_at.strftime('%d/%m/%Y %H:%M'),
            "customer": o.customer_name or "Cliente",
            "phone": o.customer_phone,
            "status": o.status,
            "status_class": st_class,
            "total": float(o.total_value or 0.0),
            "payment": o.payment_method or "-",
            "driver": o.driver.full_name if o.driver else "-",
            "delivery_type": "Mesa" if o.table_number else "Delivery",
            "items_count": len(o.items_json) if o.items_json else 0
        })

    return {
        "orders": data,
        "summary": {
            "count": total_count,
            "total_value": total_value
        },
        "has_more": (page * limit) < total_count
    }
    

@router.get("/admin/api/orders/{order_id}")
def get_single_order_api(order_id: int, db: Session = Depends(get_db), current_user: User = Depends(check_db_auth)):
    try:
        order = db.query(Order).filter(Order.id == order_id, Order.store_id == current_user.store_id).first()
        if not order: return JSONResponse(status_code=404, content={"message": "Não encontrado"})
        
        c_name = getattr(order, 'customer_name', None) or "Cliente"
        c_id = getattr(order, 'customer_id', None)
        
        # 1. Recupera e Limpa Preços (Lógica existente)
        items_safe = []
        subtotal = 0.0
        
        if order.items_json:
            for i in order.items_json:
                try: 
                    qty = float(i.get('quantity', 1))
                    price = float(i.get('price', 0))
                    
                    # Recupera preço se estiver zerado
                    if price == 0 and i.get('product_id'):
                        prod = db.query(Product).get(i['product_id'])
                        if prod: price = float(prod.price or 0)
                    
                    i['price'] = price
                    i['quantity'] = qty
                    subtotal += price * qty
                except: pass
                items_safe.append(i)
        
        # === 2. A MÁGICA: NORMALIZAÇÃO (LIMPEZA DA TRIPA) ===
        # Usamos o normalizador para quebrar o texto "X-Tudo (+Bacon)" em estrutura
        try:
            # Cria um objeto 'fake' para o normalizador não mexer no objeto real do banco agora
            class TempOrder:
                def __init__(self, items, pm, wid, eid):
                    self.items_json = items
                    self.payment_method = pm
                    self.wabiz_id = wid
                    self.external_id = eid
            
            temp_order = TempOrder(items_safe, order.payment_method, order.wabiz_id, order.external_id)
            
            # Gera a lista bonitinha
            normalized_list = normalize_order_items_for_view(temp_order)
            
            # Mescla de volta: Pega os 'details' e o 'name' limpo e injeta nos itens originais
            # (Assim mantemos os IDs e dados técnicos, mas ganhamos o visual limpo)
            if len(items_safe) == len(normalized_list):
                for idx, raw_item in enumerate(items_safe):
                    norm_item = normalized_list[idx]
                    
                    # Injeta os detalhes (Sabores, Bordas, Extras separados)
                    raw_item['details'] = norm_item.get('details', [])
                    
                    # ATENÇÃO: Substitui o título "tripa" pelo título limpo!
                    # Ex: "X-Tudo (+ Bacon)" vira apenas "X-Tudo" (o resto vai pro details)
                    if norm_item.get('name'):
                        raw_item['title'] = norm_item['name']
                        raw_item['name'] = norm_item['name']

        except Exception as e:
            print(f"⚠️ Erro ao normalizar na edição: {e}")
        # ====================================================
        
        if subtotal == 0: 
            subtotal = float(order.total_value or 0) - float(order.delivery_fee or 0) + float(order.discount or 0)

        change_val = 0.0
        pm_full = str(order.payment_method or "")
        pm_display = pm_full # Texto que será exibido

        if "Troco" in pm_full:
            try:
                import re
                # 1. Extrai valor
                match = re.search(r'R\$\s*([\d\.]+)', pm_full.split('->')[-1])
                if match: change_val = float(match.group(1))
                
                # 2. Limpa texto para exibição
                pm_display = pm_full.split('(')[0].strip()
            except: pass

        return {
            "id": order.id,
            "wabiz_id": getattr(order, 'wabiz_id', ''),
            "customer_name": c_name,
            "customer_id": c_id,
            "customer_phone": getattr(order, 'customer_phone', ''),
            "items_json": items_safe, # <--- Agora vai limpo e estruturado!
            "total_value": float(order.total_value or 0.0),
            "delivery_fee": float(order.delivery_fee or 0.0),
            "discount": float(order.discount or 0.0),
            "payment_method": pm_display,
            "payment_method_raw": pm_full,
            "address_street": getattr(order, 'address_street', ''),
            "address_number": getattr(order, 'address_number', ''),
            "address_neighborhood": getattr(order, 'address_neighborhood', ''),
            "address_city": getattr(order, 'address_city', ''),
            "address_complement": getattr(order, 'address_complement', ''),
            "notes": getattr(order, 'notes', ''),
            "status": order.status,
            "table_number": getattr(order, 'table_number', None),
            "delivery_type": getattr(order, 'delivery_type', None),
            
            "subtotal": subtotal,
            "change": change_val,
            "created_at_fmt": order.created_at.strftime('%d/%m/%Y às %H:%M') if order.created_at else "--/-- --:--"
        }
    except Exception as e:
        print(f"ERRO API DETAIL: {e}") 
        return JSONResponse(status_code=500, content={"message": str(e)})
    
    
# --- BUSCA INTELIGENTE DE CLIENTE ---
@router.get("/admin/api/customers/search")
def search_customer_api(query: str, db: Session = Depends(get_db), current_user: User = Depends(get_mixed_current_user)):
    if not query or len(query) < 3: return []
    term = f"%{query}%"
    filters = [Customer.name.ilike(term)]
    
    # Se digitar número, busca por telefone também
    clean_num = "".join(filter(str.isdigit, query))
    if len(clean_num) >= 4: filters.append(Customer.phone.ilike(f"%{clean_num}%"))
        
    customers = db.query(Customer).filter(Customer.store_id == current_user.store_id, or_(*filters)).limit(5).all()
    
    results = []
    for c in customers:
        # Pega o último endereço usado
        last_addr = db.query(Address).filter(Address.customer_id == c.id).order_by(desc(Address.last_used_at)).first()
        results.append({
            "id": c.id,
            "name": c.name,
            "phone": c.phone,
            "email": c.email if c.email else "",
            "birth_date": c.birth_date.strftime('%Y-%m-%d') if c.birth_date else "",
            # Endereço Completo para o Modal de Edição
            "street": last_addr.street if last_addr else "",
            "number": last_addr.number if last_addr else "",
            "neighborhood": last_addr.neighborhood if last_addr else "",
            "complement": last_addr.complement if last_addr else "", # <--- NOVO
            "zip_code": last_addr.zip_code if last_addr else "",     # <--- NOVO
            "city": last_addr.city if last_addr else "",             # <--- NOVO
            "state": last_addr.state if last_addr else ""            # <--- NOVO
        })
    return results


# --- GESTÃO DE TAXAS (ATUALIZADA) ---

# 1. Listar Taxas (JSON para o Modal)
@router.get("/admin/api/fees/list")
def get_fees_list(db: Session = Depends(get_db), current_user: User = Depends(check_db_auth)):
    fees = db.query(DeliveryFee).filter(DeliveryFee.store_id == current_user.store_id).order_by(DeliveryFee.neighborhood).all()
    return [{"id": f.id, "neighborhood": f.neighborhood, "fee": f.fee} for f in fees]

# 2. Salvar Taxa (Upsert por ID ou Nome)
@router.post("/admin/api/fees/save")
def save_fee(
    fee_id: Optional[int] = Form(None),
    neighborhood: str = Form(...), 
    fee: float = Form(...), 
    db: Session = Depends(get_db), 
    current_user: User = Depends(check_db_auth)
):
    neigh_clean = neighborhood.strip().upper()
    
    # Se tem ID, é edição
    if fee_id:
        fee_obj = db.query(DeliveryFee).get(fee_id)
        if fee_obj and fee_obj.store_id == current_user.store_id:
            fee_obj.neighborhood = neigh_clean
            fee_obj.fee = fee
            db.commit()
            return {"success": True}
        return JSONResponse(status_code=404, content={"message": "Taxa não encontrada"})

    # Se não tem ID, verifica se o nome já existe para evitar duplicata
    exists = db.query(DeliveryFee).filter(
        DeliveryFee.store_id == current_user.store_id, 
        DeliveryFee.neighborhood == neigh_clean
    ).first()
    
    if exists:
        # Atualiza o existente
        exists.fee = fee
    else:
        # Cria novo
        db.add(DeliveryFee(store_id=current_user.store_id, neighborhood=neigh_clean, fee=fee))
    
    db.commit()
    return {"success": True}

# 3. Excluir Taxa (Por ID)
@router.delete("/admin/api/fees/{fee_id}")
def delete_fee(fee_id: int, db: Session = Depends(get_db), current_user: User = Depends(check_db_auth)):
    fee = db.query(DeliveryFee).get(fee_id)
    if fee and fee.store_id == current_user.store_id:
        db.delete(fee)
        db.commit()
        return {"success": True}
    return JSONResponse(status_code=404, content={"message": "Erro ao excluir"})


# --- ROTA DE AÇÃO EM MASSA CORRIGIDA ---
@router.post("/admin/orders/bulk/status")
def bulk_update_status(
    background_tasks: BackgroundTasks,
    order_ids: str = Form(...), 
    new_status: str = Form(...), # <--- OBRIGATÓRIO SER 'new_status'
    db: Session = Depends(get_db),
    current_user: User = Depends(get_mixed_current_user) # Usa Cookie ou Senha
):
    try:
        # Converte string "1,2,3" para lista de inteiros
        ids_list = [int(id) for id in order_ids.split(',')]
        
        # --- CORREÇÃO: Busca pedidos para poder notificar as APIs ---
        orders_to_update = db.query(Order).filter(
            Order.store_id == current_user.store_id,
            Order.id.in_(ids_list)
        ).all()
        
        adapters = None
        if orders_to_update:
            adapters = get_active_adapters(orders_to_update[0].store)

        for order in orders_to_update:
            old_status = order.status
            order.status = new_status
            
            # Notifica Integração
            if order.external_id and adapters:
                try:
                    for adapter in adapters:
                        if isinstance(adapter, WabizAdapter):
                            process_wabiz_update(adapter, order, new_status)
                        elif isinstance(adapter, IfoodAdapter):
                            process_ifood_update(adapter, order, new_status, old_status)
                except: pass

        db.commit()
        
        # Avisa o KDS
        background_tasks.add_task(manager.broadcast, current_user.store_id, "update")
        
        return {"success": True, "message": f"{len(ids_list)} pedidos atualizados!"}
    except Exception as e:
        db.rollback()
        print(f"❌ Erro Bulk: {e}")
        return JSONResponse(status_code=500, content={"message": str(e)})


@router.post("/admin/orders/bulk/cancel")
def bulk_cancel_orders(
    background_tasks: BackgroundTasks, # <--- Adicione background_tasks aqui se não tiver
    order_ids: str = Form(...),
    reason: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_mixed_current_user)
):
    try:
        ids_list = [int(id) for id in order_ids.split(',')]
        orders = db.query(Order).filter(
            Order.store_id == current_user.store_id,
            Order.id.in_(ids_list)
        ).all()
        
        adapters = get_active_adapters(current_user.store)
        clean_reason = reason.strip()[:100]
        count = 0
        
        # --- DENTRO DO LOOP DE BULK CANCEL ---
        for order in orders:
            if order.status and "CANCELADO" in order.status: continue
            
            # Usa a nova função unificada!
            execute_cancellation_logic(db, current_user, order, clean_reason, adapters)
            count += 1
            
        db.commit()
        
        # --- PASSO 2: CHAMADAS EXTERNAS (LENTO) ---
        # Agora podemos demorar o tempo que for preciso sem travar o sistema
        for order in orders:
            try:
                pm_lower = (order.payment_method or "").lower()
                
                # Wabiz
                is_wabiz = (order.wabiz_id and not str(order.wabiz_id).startswith("M-")) or "wabiz" in pm_lower
                if is_wabiz and adapters:
                    for adapter in adapters:
                        if isinstance(adapter, WabizAdapter):
                            process_wabiz_update(adapter, order, "CANCELADO")

                # iFood
                if "ifood" in pm_lower and order.external_id and adapters:
                    for adapter in adapters:
                        if isinstance(adapter, IfoodAdapter):
                            # Lógica simplificada de código de cancelamento
                            code = "509" 
                            if "sem produto" in clean_reason.lower(): code = "503"
                            elif "cliente" in clean_reason.lower(): code = "506"
                            adapter.request_cancellation(order.external_id, code, clean_reason)
                            
            except Exception as e:
                print(f"⚠️ Erro silencioso API externa pedido {order.id}: {e}")

        background_tasks.add_task(manager.broadcast, current_user.store_id, "update")
        
        return {"success": True, "message": f"{count} pedidos cancelados."}
        
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"message": str(e)})
    

@router.post("/admin/orders/bulk/revert")
def bulk_revert_orders(
    background_tasks: BackgroundTasks, # <--- Adicione background_tasks
    order_ids: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_mixed_current_user)
):
    try:
        ids_list = [int(id) for id in order_ids.split(',')]
        orders = db.query(Order).filter(
            Order.store_id == current_user.store_id,
            Order.id.in_(ids_list)
        ).all()
        
        adapters = None
        if orders: adapters = get_active_adapters(orders[0].store)

        count = 0
        orders_to_notify = [] # Lista para guardar quem precisa de update externo

        # --- PASSO 1: BANCO ---
        for order in orders:
            current = (order.status or '').upper().strip()
            new_status = None
            old_status = order.status 
            
            if current == 'CONCLUIDO': new_status = 'ENTREGUE'
            elif current == 'ENTREGUE':
                new_status = 'SAIU_ENTREGA' if order.delivery_type == 'delivery' else 'PRONTO'
            elif current == 'SAIU_ENTREGA': new_status = 'PRONTO'
            elif current == 'PRONTO':
                new_status = 'PREPARO'
                order.kds_timer_start = datetime.now()
                
            if new_status:
                order.status = new_status
                count += 1
                orders_to_notify.append((order, new_status, old_status)) # Guarda para depois
            
        db.commit() # <--- LIBERTA O BANCO
        
        # --- PASSO 2: APIs ---
        for item in orders_to_notify:
            order, new_st, old_st = item
            if order.external_id and adapters:
                try:
                    for adapter in adapters:
                        if isinstance(adapter, WabizAdapter):
                            process_wabiz_update(adapter, order, new_st)
                        elif isinstance(adapter, IfoodAdapter):
                            process_ifood_update(adapter, order, new_st, old_st)
                except: pass # Ignora erros de conexão no revert
        
        background_tasks.add_task(manager.broadcast, current_user.store_id, "update")
        
        return {"success": True, "message": f"{count} pedidos retrocedidos."}
        
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"message": str(e)})

    
@router.post("/admin/orders/assign-driver")
def assign_driver(
    background_tasks: BackgroundTasks,
    driver_id: int = Form(...),
    order_ids: str = Form(...), 
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    try:
        ids_list = [int(id) for id in order_ids.split(',')]
        orders = db.query(Order).filter(Order.id.in_(ids_list)).all()
        
        adapters = None
        if orders: adapters = get_active_adapters(orders[0].store)

        count = 0
        orders_to_update = []

        # --- PASSO 1: BANCO ---
        for order in orders:
            old_status = order.status
            order.driver_id = driver_id
            order.status = "SAIU_ENTREGA"
            orders_to_update.append((order, old_status))
            count += 1

        db.commit() # <--- LIBERTA O BANCO
        
        # --- PASSO 2: APIs (COM LOG DE ERRO) ---
        for item in orders_to_update:
            order, old_st = item
            # Verifica se tem ID externo e adaptadores carregados
            if order.external_id and adapters:
                try:
                    for adapter in adapters:
                        if isinstance(adapter, WabizAdapter):
                            process_wabiz_update(adapter, order, "SAIU_ENTREGA")
                        elif isinstance(adapter, IfoodAdapter):
                            process_ifood_update(adapter, order, "SAIU_ENTREGA", old_st)
                except Exception as e:
                    # AGORA VAI MOSTRAR O ERRO NO TERMINAL
                    print(f"⚠️ Erro ao notificar integração no despacho: {e}")
                    import traceback
                    traceback.print_exc()

        background_tasks.add_task(manager.broadcast, current_user.store_id, "update")
        background_tasks.add_task(manager.broadcast, current_user.store_id, f"driver_update:{driver_id}")
        
        return {"success": True, "message": f"{count} pedidos despachados com sucesso!"}
    except Exception as e:
        db.rollback() # Rollback funciona porque se der erro no passo 1, nada foi salvo ainda
        return JSONResponse(status_code=500, content={"message": str(e)})
    
    
# --- AUXILIAR PARA DETECÇÃO DE MUDANÇAS ---
def normalize_signature(items_list):
    """Gera uma assinatura única dos itens para saber se houve alteração real."""
    if not items_list: return "VAZIO"
    signatures = []
    for item in items_list:
        pid = str(item.get('product_id') or item.get('id') or "0")
        try: qty = "{:.2f}".format(float(item.get('quantity', 0)))
        except: qty = "0.00"
        
        raw_obs = str(item.get('observation') or "").strip().lower()
        obs = raw_obs if raw_obs not in ["none", "null", ""] else ""
        
        raw_parts = item.get('parts') or []
        parts = sorted([str(p).strip().lower() for p in raw_parts])
        
        raw_rem = item.get('removed_ingredients') or []
        rem = sorted([str(r) for r in raw_rem])
        
        signatures.append(f"{pid}|{qty}|{obs}|{parts}|{rem}")
    return sorted(signatures)


# --- SALVAR PEDIDO (ATUALIZADO COM TROCO) ---
@router.post("/admin/orders/save")
def save_order_manual(
    background_tasks: BackgroundTasks,
    order_id: Optional[int] = Form(None),
    customer_name: str = Form(...),
    customer_phone: str = Form(None),
    delivery_type: str = Form(...),
    address_street: str = Form(None),
    address_number: str = Form(None),
    address_neighborhood: str = Form(None),
    payment_method: str = Form(...),
    change_for: Optional[float] = Form(None),
    items_json: str = Form(...),
    delivery_fee: float = Form(0.0),
    discount: float = Form(0.0),
    service_fee: float = Form(0.0),
    table_number: Optional[int] = Form(None),
    driver_tip: float = Form(0.0),
    customer_credit: float = Form(0.0),
    # Aceita string para evitar erro 422 e converte dentro
    finalize_order: Optional[str] = Form(None), 
    force_status: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_mixed_current_user),
):
    # 1. Tratamento de Booleanos (Conversão segura de string)
    is_finalizing = str(finalize_order).lower() in ('true', '1', 'yes', 'on')

    # Lógica de Taxa Automática (Bairro)
    if delivery_type == 'delivery' and delivery_fee == 0.0 and address_neighborhood:
        neigh_clean = address_neighborhood.strip().upper()
        fee_obj = db.query(DeliveryFee).filter(
            DeliveryFee.store_id == current_user.store_id,
            DeliveryFee.neighborhood == neigh_clean
        ).first()
        if fee_obj: delivery_fee = fee_obj.fee
            
    try:
        items_data = json.loads(items_json)
        
        # --- ENRIQUECIMENTO AUTOMÁTICO ---
        # Antes de salvar, garante que combos fixos tenham seus itens listados
        enrich_order_with_combo_data(db, current_user.store_id, items_data)
        # ---------------------------------
        
        # Upsert Cliente
        customer_db = None
        if customer_phone:
            customer_db = upsert_customer_smart(db, current_user.store_id, customer_phone, customer_name, None, 0)
        
        items_total = sum([float(i['price']) * float(i['quantity']) for i in items_data])    
        
        final_total = items_total + delivery_fee + service_fee - discount
        
        # --- CORREÇÃO: Limpeza preventiva do método de pagamento ---
        # Se o usuário mandou "Dinheiro (Troco...)" antigo, resetamos para "Dinheiro" limpo
        # para evitar duplicação ou texto infinito.
        if "Dinheiro" in payment_method:
            payment_method = "Dinheiro"
        
        final_payment_method = payment_method
        # Lógica original de cálculo de troco...
        if "Dinheiro" in payment_method and change_for and change_for > final_total:
             troco = change_for - final_total
             final_payment_method = f"Dinheiro (Troco p/ {change_for} -> R$ {troco:.2f})"

        now_br = get_br_time()
        
        # === CORREÇÃO: DETECTA CAIXA ABERTO PARA VINCULAR ===
        current_cash_id = get_active_cash_id(db, current_user.store_id)
        # ====================================================
        
        

        if order_id:
            order = db.query(Order).get(order_id)
            if not order: return JSONResponse(status_code=404, content={"message": "Pedido não encontrado"})


            sig_old = normalize_signature(order.items_json)
            sig_new = normalize_signature(items_data)
            items_changed = (sig_old != sig_new)
            
            # Atualiza Dados
            order.customer_name = customer_name
            order.customer_phone = customer_phone
            if customer_db: order.customer_id = customer_db.id
            order.address_street = address_street if delivery_type == 'delivery' else 'Retirada'
            order.address_number = address_number
            order.address_neighborhood = address_neighborhood
            order.payment_method = final_payment_method       
            order.items_json = items_data 
            order.total_value = final_total
            order.table_number = table_number
            order.delivery_fee = delivery_fee
            order.discount = discount
            order.service_fee = service_fee
            order.driver_tip = driver_tip
            order.customer_credit = customer_credit
            order.delivery_type = delivery_type
            # === ADICIONE ISTO AQUI (SALVA O CAIXA NA EDIÇÃO) ===
            # Se o pedido está órfão (None) e temos um caixa aberto, VINCULA AGORA!
            if order.cash_opening_id is None and current_cash_id is not None:
                order.cash_opening_id = current_cash_id
            
            # === DECISÃO DE STATUS BLINDADA ===
            
            # 1. Prioridade Máxima: Force Status (Vem da finalização de mesa)
            if force_status:
                order.status = force_status
                print(f"✅ Pedido #{order_id}: Status forçado para {force_status}")

            # 2. Finalização explícita (Delivery/Conferência)
            elif is_finalizing:
                order.status = "CONCLUIDO"
                print(f"✅ Pedido #{order_id}: Finalizado via flag.")
            
            # 3. Se os itens mudaram (Lógica Inteligente)
            elif items_changed:
                print(f"   ⚡ Mudança detectada nos itens.")
                
                # Verifica se há algum item NOVO/PENDENTE na lista
                has_pending_items = False
                for item in items_data:
                    # Se kds_done for Falso ou não existir, é um item que precisa ser feito
                    if not item.get('kds_done'):
                        has_pending_items = True
                    
                    # Garantia extra: se não tiver o campo, define como False
                    if 'kds_done' not in item:
                        item['kds_done'] = False

                # Atualiza o JSON no objeto para salvar as flags garantidas
                order.items_json = items_data
                
                # SÓ volta para PREPARO se tiver coisa nova para fazer
                if has_pending_items:
                    # CORREÇÃO: Removemos a checagem "if order.status != 'PREPARO'".
                    # Se tem item novo, SEMPRE renovamos o status e o timer para o KDS apitar.
                    order.status = 'PREPARO' 
                    order.kds_timer_start = now_br # <--- Isso faz o cartão piscar/subir no KDS
                    print(f"🔄 Pedido #{order_id}: Novos itens detectados -> PREPARO (Timer Resetado).")
                else:
                    # Se mudou texto/preço mas todos já estão prontos, mantém o status atual!
                    print(f"ℹ️ Pedido #{order_id}: Alteração apenas cadastral (Itens prontos). Status mantido: {order.status}")

        else:
            # Novo Pedido (Create)
            
            # --- CORREÇÃO: NUMERAÇÃO SEQUENCIAL CURTA (1, 2, 3...) ---
            # Define o início do dia de hoje para contar
            tz_br = pytz.timezone('America/Sao_Paulo')
            now_br_for_count = datetime.now(tz_br)
            today_start_utc = now_br_for_count.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(pytz.utc)
            
            # Conta quantos pedidos já foram feitos hoje nesta loja
            daily_count = db.query(Order).filter(
                Order.store_id == current_user.store_id,
                Order.created_at >= today_start_utc
            ).count()
            
            # O número será o próximo da fila (Ex: 1, 5, 10...)
            manual_id = str(daily_count + 1)
            # ---------------------------------------------------------
            
            # Se for Mesa/Balcão novo e já pagou tudo, pode nascer CONCLUIDO se quiser
            # Mas por padrão nasce PREPARO. Se vier force_status no Create, usamos.
            initial_status = force_status if force_status else "PREPARO"
            
            # --- CORREÇÃO DE FUSO HORÁRIO AQUI ---
            # Gravamos em UTC para que o KDS (que converte UTC->BR) mostre a hora certa
            now_utc = datetime.utcnow() 
            # -------------------------------------

            order = Order(
                store_id=current_user.store_id,
                wabiz_id=f"M-{manual_id}", 
                external_id=f"MANUAL-{manual_id}",
                created_at=now_utc,
                kds_timer_start=now_utc,
                customer_name=customer_name,
                customer_phone=customer_phone,
                customer_id=customer_db.id if customer_db else None,
                address_street=address_street if delivery_type == 'delivery' else 'Retirada',
                address_number=address_number,
                address_neighborhood=address_neighborhood,
                address_city="Local",
                address_state="UF",
                payment_method=final_payment_method,
                items_json=items_data,
                total_value=final_total,
                delivery_fee=delivery_fee,
                discount=discount,
                service_fee=service_fee,
                status=initial_status,
                table_number=table_number,
                driver_tip=driver_tip,
                customer_credit=customer_credit,
                delivery_type=delivery_type,
                cash_opening_id = current_cash_id,
            )
            db.add(order)
            
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(order, "items_json")
        
        db.commit()
        
        # --- INICIO DA CORREÇÃO (Avisar Wabiz se foi Concluído) ---
        # Se o status final for de conclusão (ENTREGUE/CONCLUIDO) e tiver ID externo
        if order.status in ['CONCLUIDO', 'ENTREGUE'] and order.external_id:
            try:
                # Importa dentro para evitar ciclo se necessário, ou usa os já importados no topo
                def notify_finish():
                    adapters = get_active_adapters(order.store)
                    for adapter in adapters:
                        if isinstance(adapter, WabizAdapter):
                            process_wabiz_update(adapter, order, order.status)
                        # Se quiser iFood também:
                        elif isinstance(adapter, IfoodAdapter):
                            # Para iFood, precisamos do status antigo, mas aqui assumimos fluxo de finalização
                            process_ifood_update(adapter, order, order.status, "SAIU_ENTREGA") 

                background_tasks.add_task(notify_finish)
            except Exception as e:
                print(f"⚠️ Erro ao notificar finalização no save_order: {e}")
        # --- FIM DA CORREÇÃO ---

        # Envia evento de atualização para atualizar as telas (Mesas/Kanban)
        background_tasks.add_task(manager.broadcast, current_user.store_id, "update")
        
        return {"success": True}

    except Exception as e:
        db.rollback()
        print(f"❌ ERRO SAVE: {str(e)}")
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"message": str(e)})
    

@router.post("/admin/orders/{order_id}/status")
def update_order_status(
    order_id: int,
    background_tasks: BackgroundTasks, # <--- MUDOU PARA CÁ (Antes dos itens com '=')
    status: str = Form(...), 
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    # 1. PARTE CRÍTICA: Atualizar Banco de Dados
    # Se isso falhar, tem que dar erro mesmo para você saber.
    order = db.query(Order).get(order_id)
    if not order: return JSONResponse(status_code=404, content={"message": "Não encontrado"})
    
    old_status = order.status
    order.status = status
    db.commit() # Salva o novo status
    
    # 2. PARTE NÃO-CRÍTICA: Notificações
    # Envolvemos tudo em um Try/Except genérico.
    # Se qualquer coisa aqui falhar, o pedido JÁ ESTÁ SALVO como "PRONTO" e o sistema não trava.
    try:
        # A. Notificar Integrações (iFood/Wabiz)
        if order.external_id:
            adapters = get_active_adapters(order.store)
            for adapter in adapters:
                if isinstance(adapter, WabizAdapter):
                    process_wabiz_update(adapter, order, status)
                elif isinstance(adapter, IfoodAdapter):
                    process_ifood_update(adapter, order, status, old_status)
        
        # B. Notificar WhatsApp (Retirada)
        if status == 'PRONTO':
            # Proteção contra valor Nulo no endereço
            address_check = (order.address_street or "").lower()
            is_pickup = "retirada" in address_check or order.delivery_type == 'balcao'
            
            if is_pickup and order.customer_phone:
                background_tasks.add_task(
                    notify_pickup_ready,
                    store=order.store,
                    customer_phone=order.customer_phone,
                    customer_name=order.customer_name or "Cliente",
                    order_id=order.wabiz_id or str(order.id)
                )
                
    except Exception as e:
        # Se der erro, apenas "printa" no console do servidor, mas o cliente segue feliz
        print(f"⚠️ Erro silencioso ao notificar pedido #{order_id}: {str(e)}")

    # Dispara atualização do KDS (Socket)
    background_tasks.add_task(manager.broadcast, current_user.store_id, "update")
    
    return {"success": True}


@router.delete("/admin/orders/{order_id}")
async def delete_order(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_mixed_current_user)
):
    order = db.query(Order).get(order_id)
    if not order or order.store_id != current_user.store_id:
        return JSONResponse(status_code=403, content={"message": "Erro"})
    
    db.delete(order)
    db.commit()
    
    asyncio.create_task(manager.broadcast(current_user.store_id, "update"))
    
    return {"success": True}


@router.post("/admin/orders/{order_id}/close-account")
async def close_account(
    order_id: int,
    payment_method: str = Form(...),
    service_fee: float = Form(0.0),
    discount: float = Form(0.0),
    total_paid: float = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_mixed_current_user)
):
    order = db.query(Order).get(order_id)
    if not order: return JSONResponse(status_code=404, content={"message": "Pedido não encontrado"})
    
    # Atualiza valores finais
    order.payment_method = payment_method
    order.service_fee = service_fee
    order.discount = discount
    order.total_value = total_paid
    
    # --- CORREÇÃO: Status definitivo de conclusão ---
    order.status = "CONCLUIDO" 
    
    db.commit()
    
    # Avisa todas as telas para removerem a mesa da lista
    await manager.broadcast(current_user.store_id, "update")
    
    return {"success": True}


# EROTA PARA CANCELAMENTO DE PEDIDOS

@router.post("/admin/orders/{order_id}/cancel")
async def cancel_order_endpoint(
    order_id: int,
    reason: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    # Busca o pedido alvo
    target_order = db.query(Order).get(order_id)
    
    if not target_order or target_order.store_id != current_user.store_id:
        return JSONResponse(status_code=404, content={"message": "Pedido não encontrado."})
    
    clean_reason = reason.strip()[:100]
    
    # LISTA DE ALVOS: Começa com o pedido atual
    orders_to_cancel = [target_order]
    
    # --- LIMPEZA DE MESA (ZUMBIS) ---
    # Se for mesa, cancela todos os pedidos abertos daquela mesa
    if target_order.table_number:
        zombies = db.query(Order).filter(
            Order.store_id == current_user.store_id,
            Order.table_number == target_order.table_number,
            Order.status != 'ENTREGUE',
            not_(Order.status.ilike('CANCELADO%')),
            Order.id != order_id
        ).all()
        if zombies:
            orders_to_cancel.extend(zombies)

    # Carrega adaptadores uma única vez
    adapters = get_active_adapters(target_order.store)

    # Executa cancelamento e estorno para todos
    for order in orders_to_cancel:
        if order.status and "CANCELADO" in order.status: continue
        
        # Usa a mesma função unificada!
        execute_cancellation_logic(db, current_user, order, clean_reason, adapters)
    
    db.commit()
    
    asyncio.create_task(manager.broadcast(current_user.store_id, "update"))
    
    return {"success": True, "message": "Pedido cancelado localmente e nas plataformas."}



@router.post("/admin/resend/{wabiz_id}")
def resend_order_event(wabiz_id: str, target: str, db: Session = Depends(get_db), current_user: User = Depends(check_db_auth)):
    order = db.query(Order).filter(Order.store_id == current_user.store_id, Order.wabiz_id == wabiz_id).first()
    if not order: return JSONResponse(status_code=404, content={"success": False})
    store = order.store

    user_phone = normalize_phone(order.customer_phone)
    client_ip = recover_historical_ip(db, store.id, user_phone, order.customer_email)
    items_clean = order.items_json or [] 
    
    upsert_customer_smart(db, store.id, user_phone, order.customer_name, order.customer_email, 0)
    
    user_data_pixel = {
        "email": order.customer_email, "phone": user_phone, "name": order.customer_name,
        "ip": client_ip, "user_agent": "WabizManualResend", "city": order.address_city
    }
    
    results = dispatch_smart_event(
        store, "Purchase", user_data_pixel, items_clean, order.total_value, wabiz_id
    )
    
    success = False
    if target == 'facebook':
        if results['fb']: 
            order.sent_to_facebook = True
            success = True
    elif target == 'google':
        if results['ga']:
            order.sent_to_google = True
            success = True

    db.commit()
    return {"success": success}

# --- ROTA WEBSOCKET ADMIN (COM CORREÇÃO DE EXPIRAÇÃO) ---
@router.websocket("/ws/orders")
async def orders_websocket(
    websocket: WebSocket, 
    token: Optional[str] = Query(None), 
    db: Session = Depends(get_db)
):
    store_id = None
    
    # 1. Tenta pegar token da URL (Prioridade) ou Cookie
    auth_token = token
    if not auth_token:
        cookie = websocket.cookies.get("access_token")
        if cookie and "Bearer " in cookie:
            auth_token = cookie.split(" ")[1]

    # Se não tiver token, rejeita
    if not auth_token:
        print("❌ WS: Conexão rejeitada (Sem token).")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    try:
        # --- AQUI ESTÁ A CORREÇÃO (options={'verify_exp': False}) ---
        # Isso permite que um monitor KDS fique ligado o dia todo sem ser desconectado 
        # quando o token "oficial" expira, desde que a assinatura seja válida.
        payload = jwt.decode(
            auth_token, 
            SECRET_KEY, 
            algorithms=[ALGORITHM], 
            options={"verify_exp": False} 
        )
        
        user_email = payload.get("sub")
        if user_email:
            user = db.query(User).filter(User.email == user_email).first()
            if user: 
                store_id = user.store_id
                
    except Exception as e:
        print(f"❌ WS: Erro Token: {e}")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if not store_id:
        print("❌ WS: Usuário sem loja.")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # Conexão aceita
    await manager.connect(websocket, store_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, store_id)
    except Exception as e:
        print(f"❌ WS: Erro Conexão: {e}")
        manager.disconnect(websocket, store_id)
        
        
# --- API DE FIADO / DÍVIDA DO CLIENTE ---
@router.get("/admin/api/customer/{cust_id}/debt")
def get_customer_debt(
    cust_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    customer = db.query(Customer).get(cust_id)
    if not customer or customer.store_id != current_user.store_id:
        return JSONResponse(status_code=404, content={"message": "Cliente não encontrado"})

    # Busca pedidos com pagamento "Fiado" vinculados a este cliente (pelo telefone)
    # Ignora pedidos cancelados
    orders = db.query(Order).filter(
        Order.store_id == current_user.store_id,
        Order.customer_phone == customer.phone,
        Order.payment_method.ilike("%Fiado%"),
        not_(Order.status.ilike("%CANCELADO%"))
    ).order_by(desc(Order.created_at)).all()

    debt_list = []
    total_debt = 0.0

    for o in orders:
        val = o.total_value or 0.0
        total_debt += val
        
        debt_list.append({
            "id": o.id,
            "wabiz_id": o.wabiz_id or str(o.id),
            "date": o.created_at.strftime("%d/%m/%Y %H:%M"),
            "total": val,
            "items": o.items_json or []
        })

    return {
        "client_name": customer.name,
        "total_debt": total_debt,
        "orders": debt_list
    }
    
# --- FUNÇÃO ÚNICA DE CANCELAMENTO (SEGURANÇA E MANUTENÇÃO) ---
def execute_cancellation_logic(db, user, order, reason_text, adapters):
    """Centraliza: Estorno de Estoque + APIs Externas + Status Local"""
    
    # 1. Estorno de Estoque (Se tiver itens)
    if order.items_json:
        try:
            # Chama a função que já existe no stock_engine
            return_stock_from_order(db, user.store_id, order.items_json, order.wabiz_id or str(order.id))
        except Exception as e:
            print(f"⚠️ Erro parcial ao estornar estoque pedido {order.id}: {e}")

    # 2. Comunicação com APIs (iFood / Wabiz)
    pm_lower = (order.payment_method or "").lower()
    
    # Wabiz
    is_wabiz = (order.wabiz_id and not str(order.wabiz_id).startswith("M-")) or "wabiz" in pm_lower
    if is_wabiz and adapters:
        for adapter in adapters:
            if isinstance(adapter, WabizAdapter):
                try: process_wabiz_update(adapter, order, "CANCELADO")
                except: pass

    # iFood (Com mapeamento inteligente de motivos)
    if "ifood" in pm_lower and order.external_id and adapters:
        for adapter in adapters:
            if isinstance(adapter, IfoodAdapter):
                try:
                    code = "509" # Genérico
                    r_lower = reason_text.lower()
                    if any(x in r_lower for x in ["sem produto", "indisponivel", "falta"]): code = "503"
                    elif any(x in r_lower for x in ["fechado", "encerrado"]): code = "513"
                    elif any(x in r_lower for x in ["cliente", "desistiu"]): code = "506"
                    
                    adapter.request_cancellation(order.external_id, code, reason_text)
                except: pass
    
    # 3. Atualização Local
    order.status = f"CANCELADO ({reason_text})"
    return True

# --- ROTA DE SOLICITAÇÃO DE CONTA (GARÇOM) ---
@router.post("/api/waiter/request-bill/{table_number}")
async def waiter_request_bill(
    table_number: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_waiter) # Garante que é um garçom logado
):
    # Busca se tem pedido aberto na mesa
    order = db.query(Order).filter(
        Order.store_id == current_user.store_id,
        Order.table_number == table_number,
        not_(Order.status.in_(['ENTREGUE', 'CONCLUIDO', 'FINALIZADO'])),
        not_(Order.status.ilike('%CANCELADO%'))
    ).first()

    if not order:
        return JSONResponse(status_code=404, content={"message": "Nenhum pedido aberto nesta mesa."})

    # Envia sinal para o WebSocket
    # Formato: bill_req:MESA:ORDER_ID:NOME_GARCOM
    waiter_name = current_user.full_name.split()[0]
    msg = f"bill_req:{table_number}:{order.id}:{waiter_name}"
    
    await manager.broadcast(current_user.store_id, msg)

    return {"success": True, "message": "Solicitação enviada ao caixa."}

    
# --- ROTA DE IMPRESSÃO DE CUPOM ---
    
@router.get("/admin/print/order/{order_id}", response_class=HTMLResponse)
def print_order_receipt(
    order_id: int, 
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    order = db.query(Order).get(order_id)
    if not order or order.store_id != current_user.store_id:
        return "Pedido não encontrado"
        
    now_fmt = datetime.now().strftime("%d/%m/%Y %H:%M")
    
    # --- CORREÇÃO: Usamos o Normalizador para limpar a 'tripa' ---
    # Isso separa o Título dos Sabores/Adicionais
    clean_items = normalize_order_items_for_view(order)
    # -------------------------------------------------------------
    
    plat = "PDV"
    if "ifood" in (order.payment_method or "").lower(): plat = "iFood"
    elif order.wabiz_id and not order.wabiz_id.startswith("M-"): plat = "Wabiz"
    order.platform = plat

    return templates.TemplateResponse("receipt.html", {
        "request": {}, 
        "order": order,
        "items": clean_items, # <--- Passamos a lista limpa para o HTML
        "now": now_fmt
    })

@router.get("/admin/api/print-text/{order_id}")
def get_order_text_format(
    order_id: int, 
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    order = db.query(Order).get(order_id)
    if not order: return JSONResponse(status_code=404, content={"error": "Nao encontrado"})
    
    # --- FORMATAÇÃO DE TEXTO PARA IMPRESSORA TÉRMICA ---
    # \x1B\x45\x01 = Negrito ON | \x1B\x45\x00 = Negrito OFF
    
    txt = ""
    txt += "================================\n"
    txt += f"      ALIV PIZZARIA\n"
    txt += "================================\n\n"
    
    txt += f"PEDIDO: #{order.wabiz_id}\n"
    txt += f"DATA:   {order.created_at.strftime('%d/%m %H:%M')}\n"
    txt += f"CLIENTE: {order.customer_name}\n"
    txt += f"TEL:     {order.customer_phone}\n\n"
    
    if order.delivery_type == 'delivery':
        txt += "--------------------------------\n"
        txt += "           ENTREGA\n"
        txt += "--------------------------------\n"
        txt += f"{order.address_street}, {order.address_number}\n"
        txt += f"{order.address_neighborhood}\n"
        if order.address_complement: txt += f"Obs: {order.address_complement}\n"
    else:
        txt += "--------------------------------\n"
        txt += "       RETIRADA / MESA\n"
        txt += "--------------------------------\n"

    txt += "\nITENS:\n"
    
    # --- CORREÇÃO AQUI TAMBÉM ---
    # Usa o normalizador para quebrar as linhas corretamente
    items_clean = normalize_order_items_for_view(order)
    
    for item in items_clean:
        qty = int(item.get('quantity', 1))
        name = item.get('name', 'Item') # Nome já vem limpo (ex: "Pizza Grande")
        
        txt += f"{qty}x {name}\n"
        
        # Itera sobre os detalhes (Sabores, Bordas, Extras)
        if item.get('details'):
            for det in item['details']:
                # Indentação para ficar bonito
                txt += f"   {det['text']}\n"
        
        # Remoções
        if item.get('removed'):
            for rem in item['removed']:
                txt += f"   (-) Sem {rem}\n"
                
        # Observação do Item
        if item.get('observation'):
            txt += f"   (Obs: {item['observation']})\n"
            
        txt += "-\n" # Separador sutil entre itens
                
    txt += "\n--------------------------------\n"
    if order.delivery_fee > 0: txt += f"TAXA:    R$ {order.delivery_fee:.2f}\n"
    if order.discount > 0:     txt += f"DESC:    R$ {order.discount:.2f}\n"
    txt += f"TOTAL:   R$ {order.total_value:.2f}\n"
    txt += "--------------------------------\n"
    txt += f"PAGTO: {order.payment_method}\n"
    
    if order.driver_tip > 0:
        txt += f"CAIXINHA: R$ {order.driver_tip:.2f}\n"
        
    if order.notes:
        txt += f"\nOBS PEDIDO: {order.notes}\n"

    txt += "\n\n\n" 

    return {"text": txt}