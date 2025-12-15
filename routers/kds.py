from fastapi import (
    APIRouter,
    Request,
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    Query,
    status,
)
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy import func, or_, desc
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from datetime import datetime, timedelta
import pytz
import traceback
import re
from typing import Optional
from jose import jwt

# Imports locais
from database import get_db
from models import User, Order, Product, ProductMapping, Category, ProductionSector
from dependencies import templates, check_db_auth
from services.sockets import manager
from auth import ALGORITHM, SECRET_KEY, verify_password, create_access_token
import unicodedata
from services.background_jobs import get_active_adapters
from services.integrations.wabiz import process_wabiz_update, WabizAdapter
from services.integrations.ifood import process_ifood_update, IfoodAdapter
from fastapi import BackgroundTasks

# --- IMPORTANTE: IMPORTA O SEU NOVO NORMALIZADOR ---
from services.normalizer import normalize_order_items_for_view

router = APIRouter()

# --- TELA INICIAL E MONITOR ---
@router.get("/kds", response_class=HTMLResponse)
def kds_landing(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    return templates.TemplateResponse(
        "kds.html", {"request": request, "mode": "select", "user": current_user}
    )

@router.get("/kds/{mode}", response_class=HTMLResponse)
def kds_monitor(
    mode: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    if mode not in ["kitchen", "expedition"]:
        return RedirectResponse("/kds")
    token = create_access_token(data={"sub": current_user.email})
    return templates.TemplateResponse(
        "kds.html",
        {"request": request, "mode": mode, "user": current_user, "access_token": token},
    )

# --- API: BUSCA DE PEDIDOS INTELIGENTE ---
@router.get("/api/kds/orders")
def get_kds_orders(
    mode: str,
    sector_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    try:
        store_id = current_user.store_id
        
        # --- 1. CORREÇÃO DE FUSO HORÁRIO (Definição) ---
        utc = pytz.utc
        br_zone = pytz.timezone("America/Sao_Paulo")
        now_aware = datetime.now(br_zone) # Agora (Brasil)
        # -----------------------------------------------

        # Configuração do Setor
        has_expedition = True
        if sector_id:
            try:
                sector = db.query(ProductionSector).get(sector_id)
                if sector: has_expedition = sector.has_expedition
            except: pass

        # Status Alvo
        if mode == "kitchen":
            target_status = ["PREPARO", "FORNO"]
        elif mode == "expedition":
            if not has_expedition: return []
            target_status = ["FORNO", "EXPEDICAO", "PRONTO"]
        else:
            return []

        # Busca Pedidos (Limite 24h para segurança)
        limit_date = datetime.now() - timedelta(hours=24)
        
        orders = (
            db.query(Order)
            .filter(
                Order.store_id == store_id,
                Order.status.in_(target_status),
                Order.created_at >= limit_date,
            )
            .order_by(Order.created_at.asc())
            .all()
        )

        data = []
        
        # Caches
        cat_sector_map = {c.id: c.sector_id for c in db.query(Category).filter_by(store_id=store_id).all()}
        prod_cat_map = {p.name.lower().strip(): p.category_id for p in db.query(Product).filter_by(store_id=store_id).all() if p.name}

        for o in orders:
            try:
                # Normaliza
                all_items = normalize_order_items_for_view(o)
                visible_items = []

                for item in all_items:
                    # Lógica de Filtro por Setor (Resumida para focar na correção do tempo)
                    if sector_id:
                        cat_id = None
                        raw_title = str(item.get("name") or "").lower()
                        clean = raw_title.replace("combo:", "").split(":")[0].strip()
                        clean = re.sub(r"^\d+x\s+", "", clean).strip()

                        cat_id = prod_cat_map.get(clean)
                        if not cat_id:
                            for db_name, db_cat_id in prod_cat_map.items():
                                if db_name in clean or clean in db_name:
                                    cat_id = db_cat_id
                                    break

                        if cat_id:
                            item_sector = cat_sector_map.get(cat_id)
                            if item_sector and item_sector != sector_id: continue
                            if not item_sector and sector_id != 1: continue
                        else:
                            if sector_id != 1: continue
                            forbidden = ["refri", "cerveja", "coca", "guarana", "fanta", "pepsi", "suco", "agua", "h2oh", "lata", "2l", "600ml", "vinho"]
                            if any(k in str(item.get("name") or "").lower() for k in forbidden): continue

                    # Filtro de Estágio KDS
                    stage = int(item.get("kds_stage", 0) if "kds_stage" in item else 0)
                    if item.get("kds_done", False): continue

                    if not has_expedition: 
                        if stage == 0: visible_items.append(item)
                    else: 
                        if mode == "kitchen" and stage == 0: visible_items.append(item)
                        elif mode == "expedition" and stage == 1: visible_items.append(item)

                if not visible_items and sector_id: continue
                if not sector_id: visible_items = all_items
                if not visible_items: continue

                # --- 2. CÁLCULO DE TEMPO CORRIGIDO (O Pulo do Gato) ---
                # Pega a data do banco (UTC)
                raw_dt = getattr(o, "kds_timer_start", None) or o.created_at
                
                if raw_dt:
                    # Se vier sem timezone (naive), marca como UTC
                    if raw_dt.tzinfo is None:
                        raw_dt = utc.localize(raw_dt)
                    
                    # Converte para Horário do Brasil
                    dt_br = raw_dt.astimezone(br_zone)
                else:
                    dt_br = now_aware

                # Calcula minutos reais
                elapsed = int((now_aware - dt_br).total_seconds() / 60)
                
                # Hora formatada
                time_str = dt_br.strftime("%H:%M")
                # -----------------------------------------------------

                btn_action = "advance"
                btn_label = "ENVIAR P/ FORNO 🔥"

                if mode == "expedition":
                    btn_action = "finish"
                    btn_label = "PRONTO / EXPEDIR ✅"
                    if o.delivery_type == 'delivery' or (o.address_street and 'retirada' not in o.address_street.lower()):
                         btn_label = "DESPACHAR 🛵"
                elif not has_expedition:
                    btn_action = "finish"
                    btn_label = "PRONTO ✅"

                pm_lower = (o.payment_method or "").lower()
                platform_name = "PDV"
                if "ifood" in pm_lower: platform_name = "IFOOD"
                elif "wabiz" in pm_lower or (o.wabiz_id and not str(o.wabiz_id).startswith("M-")): platform_name = "WABIZ"

                data.append({
                    "id": o.id,
                    "wabiz_id": o.wabiz_id or str(o.id),
                    "customer": (o.customer_name or "Cliente").split()[0],
                    "items": visible_items,
                    "obs": o.notes if o.notes else "",
                    "time": time_str,    # Hora BR
                    "elapsed": elapsed,  # Tempo real
                    "status": o.status,
                    "btn_action": btn_action,
                    "btn_label": btn_label,
                    "type": "MESA" if o.table_number else "DELIVERY",
                    "table": o.table_number, 
                    "platform": platform_name,
                    "css_class": getattr(o, "css_class", ""),
                })

            except Exception as e:
                print(f"KDS Error Order {o.id}: {e}")
                continue

        return data

    except Exception as e:
        print("❌ ERRO FATAL API KDS:")
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"message": str(e)})
    
    
# --- AÇÃO: AVANÇAR / FINALIZAR (Mantida, pois altera o status no banco) ---
@router.post("/api/kds/advance/{order_id}")
async def advance_order_status(
    order_id: int,
    current_status: str,
    background_tasks: BackgroundTasks, # <--- NOVO PARÂMETRO
    sector_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    order = db.query(Order).get(order_id)
    if not order: return {"success": False}

    # Salva o status antigo para comparação
    old_status = order.status

    sector_has_expedition = True
    if sector_id:
        sec = db.query(ProductionSector).get(sector_id)
        if sec: sector_has_expedition = sec.has_expedition

    import copy
    items = copy.deepcopy(order.items_json) if order.items_json else []

    # Mapas para identificar itens deste setor
    cat_sector_map = {c.id: c.sector_id for c in db.query(Category).filter_by(store_id=order.store_id).all()}
    prod_cat_map = {p.name.lower().strip(): p.category_id for p in db.query(Product).filter_by(store_id=order.store_id).all() if p.name}

    def is_item_in_sector(item):
        if not sector_id: return True
        raw_title = str(item.get("title") or item.get("name") or "").lower()
        clean = raw_title.replace("combo:", "").split(":")[0].strip()
        clean = re.sub(r"^\d+x\s+", "", clean).strip()
        cat_id = prod_cat_map.get(clean)
        if cat_id:
            s_id = cat_sector_map.get(cat_id)
            if s_id == sector_id: return True
            if not s_id and sector_id == 1: return True
        elif sector_id == 1: return True
        return False

    items_changed = False

    if current_status == "kitchen":
        for item in items:
            if is_item_in_sector(item):
                if int(item.get("kds_stage", 0)) == 0:
                    if sector_has_expedition: item["kds_stage"] = 1
                    else:
                        item["kds_stage"] = 2
                        item["kds_done"] = True
                    items_changed = True

    elif current_status == "expedition":
        for item in items:
            if is_item_in_sector(item):
                if int(item.get("kds_stage", 0)) == 1:
                    item["kds_stage"] = 2
                    item["kds_done"] = True
                    items_changed = True

    if items_changed:
        order.items_json = items        
        flag_modified(order, "items_json")

        all_done = True
        any_oven = False
        for i in items:
            s = int(i.get("kds_stage", 0))
            if s < 2: all_done = False
            if s == 1: any_oven = True

        # Define o novo Status Global do Pedido
        if all_done: order.status = "PRONTO"
        elif any_oven: order.status = "FORNO"
        else: order.status = "PREPARO"

        db.commit()
        await manager.broadcast(order.store_id, "update")
        
        # --- CORREÇÃO: DISPARA INTEGRAÇÕES (Wabiz/iFood) ---
        # Só avisa se o status mudou (Ex: foi de PREPARO para PRONTO) e tem vínculo externo
        if order.status != old_status and order.external_id:
            
            def notify_integrations():
                try:
                    adapters = get_active_adapters(order.store)
                    for adapter in adapters:
                        if isinstance(adapter, WabizAdapter):
                            # A função process_wabiz_update já sabe enviar Status 4 se for Balcão/PRONTO
                            process_wabiz_update(adapter, order, order.status)
                            
                        elif isinstance(adapter, IfoodAdapter):
                            process_ifood_update(adapter, order, order.status, old_status)
                except Exception as e:
                    print(f"⚠️ [KDS] Erro notificação externa: {e}")

            background_tasks.add_task(notify_integrations)
        # ---------------------------------------------------

    return {"success": True}


def normalize_text_match(text):
    """Remove acentos e minúsculas para comparação"""
    if not text: return ""
    return unicodedata.normalize('NFKD', text).encode('ASCII', 'ignore').decode('ASCII').lower()

@router.get("/api/product-details")
def get_prod_details(
    name: str, 
    code: Optional[str] = None, 
    product_id: Optional[str] = None, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(check_db_auth)
):
    prod = None
    
    # Normaliza nome para comparação
    raw_name_clean = name.split(":")[1].strip() if ":" in name else name
    raw_name_clean = raw_name_clean.split("(")[0].strip()
    raw_name_clean = raw_name_clean.replace("½", "").replace("1/2", "").strip()
    search_term = normalize_text_match(raw_name_clean)

    # Helper de Verificação de Nome
    def check_name_match(candidate_prod):
        cand_name = normalize_text_match(candidate_prod.name)
        cand_tokens = set(cand_name.split())
        search_tokens = set(search_term.split())
        stop_words = {'de', 'com', 'e', 'a', 'o', 'da', 'do', 'pizza', 'esfiha', 'borda', 'combo'}
        relevant_cand = cand_tokens - stop_words
        relevant_search = search_tokens - stop_words
        
        # Aceita se tiver match parcial ou nome exato
        return not relevant_search.isdisjoint(relevant_cand) or candidate_prod.name.lower() == raw_name_clean.lower()

    # 1. Tenta ID INTERNO (Prioridade PDV)
    if product_id and str(product_id).isdigit() and int(product_id) > 0:
        candidate = db.query(Product).filter(
            Product.id == int(product_id), 
            Product.store_id == current_user.store_id
        ).first()
        
        if candidate and check_name_match(candidate):
            prod = candidate

    # 2. Tenta MAPEAMENTO (External Code)
    # Se o passo 1 falhou ou foi rejeitado
    codes_to_try = []
    if code and code not in ["None", "", "null"]: codes_to_try.append(str(code))
    
    # Tenta usar o product_id como código externo (caso venha trocado)
    if not prod and product_id and product_id not in codes_to_try: 
        codes_to_try.append(str(product_id))

    if not prod and codes_to_try:
        mapping = db.query(ProductMapping).filter(
            ProductMapping.store_id == current_user.store_id, 
            ProductMapping.external_code.in_(codes_to_try)
        ).first()
        
        if mapping:
            # CORREÇÃO: Confia no Código Externo (Prioridade)
            # Se o código bateu (ex: 140), usamos o produto, mesmo que o nome no pedido seja genérico (ex: "BEBIDAS")
            prod = mapping.product
            
            # Apenas loga aviso se o nome for muito diferente, para auditoria, mas não bloqueia
            if not check_name_match(mapping.product):
                 print(f"ℹ️ [KDS] Match por Código {mapping.external_code}: '{name}' -> '{mapping.product.name}' (Nomes diferentes, mas aceito).")

    # 3. Tenta NOME (Último Recurso)
    if not prod:
        # Tenta exato
        prod = db.query(Product).filter(
            Product.store_id == current_user.store_id, 
            func.lower(Product.name) == raw_name_clean.lower()
        ).first()
        
        # Tenta parcial
        if not prod:
             prod = (db.query(Product)
                    .filter(Product.store_id == current_user.store_id, Product.name.ilike(f"%{raw_name_clean}%"))
                    .order_by(func.length(Product.name).asc()) 
                    .first())
    
    if not prod: return {"found": False}

    recipe = []
    if prod.recipe_items:
        for r in sorted(prod.recipe_items, key=lambda x: x.quantity, reverse=True):
            if r.ingredient:
                qty_show = f"{r.quantity:.0f}" if r.quantity.is_integer() else f"{r.quantity:.2f}"
                line = f"<b>{qty_show} {r.ingredient.unit_rel.name if r.ingredient.unit_rel else 'UN'}</b> de {r.ingredient.name}"
                recipe.append(line)
                
    return {"found": True, "name": prod.name, "description": prod.description or "","image": prod.image_url, "prep_method": prod.preparation_method, "recipe": recipe}


@router.websocket("/ws/kds")
async def kds_websocket(websocket: WebSocket, token: Optional[str] = Query(None), db: Session = Depends(get_db)):
    store_id = None
    auth_token = token
    if not auth_token:
        cookie = websocket.cookies.get("access_token")
        if cookie and "Bearer " in cookie: auth_token = cookie.split(" ")[1]
    if not auth_token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        payload = jwt.decode(auth_token, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_exp": False})
        user = db.query(User).filter(User.email == payload.get("sub")).first()
        if user: store_id = user.store_id
    except: pass
    if not store_id:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await manager.connect(websocket, store_id)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect: manager.disconnect(websocket, store_id)
    

#ROTA PARA BUSCAR PEDIDOS FINALIZADOS (REIMPRESSÃO DE ETIQUETA)
@router.post("/api/internal/kds-trigger/{store_id}")
async def trigger_kds_update(store_id: int):
    await manager.broadcast(store_id, "new_order")
    return {"status": "sinal_enviado"}

# --- NOVA ROTA: HISTÓRICO RECENTE PARA KDS ---
@router.get("/api/kds/history")
def get_kds_history_recent(
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    """Retorna últimos finalizados com Hora Brasil e Lista Completa de Itens"""
    orders = db.query(Order).filter(
        Order.store_id == current_user.store_id,
        Order.status.in_(['PRONTO', 'ENTREGUE', 'CONCLUIDO', 'SAIU_ENTREGA']) 
    ).order_by(desc(Order.created_at)).limit(limit).all()

    data = []
    utc = pytz.utc
    br_zone = pytz.timezone("America/Sao_Paulo")

    for o in orders:
        # 1. Correção de Fuso Horário
        raw_dt = o.created_at
        if raw_dt.tzinfo is None:
            raw_dt = utc.localize(raw_dt)
        local_dt = raw_dt.astimezone(br_zone)
        
        # 2. Normalização Completa dos Itens (Estrutura Rica)
        items_norm = normalize_order_items_for_view(o)
            
        data.append({
            "id": o.id,
            "wabiz_id": o.wabiz_id,
            "customer": (o.customer_name or "Cliente").split()[0],
            "time": local_dt.strftime("%H:%M"), 
            "status": o.status,
            "items": items_norm # <--- Enviando a lista completa, não string
        })
    return data


# --- NOVO ENDPOINT: GERAR ETIQUETA ZEBRA (40x25mm) ---
@router.get("/api/kds/zpl/{order_id}")
def get_order_zpl_label(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    order = db.query(Order).get(order_id)
    if not order:
        return JSONResponse(status_code=404, content={"message": "Pedido não encontrado"})
    
    # Identificador para imprimir (Wabiz ID ou ID Interno)
    display_id = order.wabiz_id or str(order.id)
    if str(display_id).startswith("M-"):
        display_id = display_id.replace("M-", "") # Limpa prefixo manual se tiver
    
    # --- CÓDIGO ZPL PARA 40x25mm (Aprox 320x200 dots) ---
    # ^XA = Início
    # ^PW320 = Largura da etiqueta em pontos
    # ^LL200 = Altura da etiqueta
    # ^FO = Posição (X, Y)
    # ^A0N = Fonte Padrão
    # ^FB = Caixa de Texto (Largura, Linhas, Espaço, Alinhamento Centro)
    # ^FD = Campo de Dados (O número)
    # ^XZ = Fim
    
    zpl = (
        "^XA"
        "^PW320"
        "^LL200"
        "^FO0,50"
        "^A0N,100,100" 
        "^FB320,1,0,C,0"
        f"^FD{display_id}^FS" 
        "^XZ"
    )
    
    return {"zpl": zpl}

