import os
import time
from fastapi import (
    FastAPI,
    Request,
    Depends,
    HTTPException,
    status,
    Query,
    Form,
    UploadFile,
    File,
)
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
import json
from datetime import datetime, timedelta
from passlib.context import CryptContext
from fastapi.security import OAuth2PasswordBearer
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from sqlalchemy import desc, func, or_, cast, String, not_
from sqlalchemy.orm import Session
from services.sockets import manager

# Importa o adaptador e o processador
from services.integrations.wabiz import WabizAdapter
from services.background_jobs import process_standard_order, get_active_adapters


# --- Imports Locais ---
from database import Base, engine, get_db, SessionLocal
from auth import (
    verify_password,
    get_password_hash,
    create_access_token,
    SECRET_KEY,
    ALGORITHM,
)
from models import (
    User,
    Store,
    Campaign,
    Insight,
    Customer,
    Address,
    DriverSession,
    DriverAdvance,
    Event,
    Order,
)

# --- NOVOS IMPORTS (REFATORADOS) ---
from dependencies import (
    security,
    templates,
    check_db_auth,
    check_role,
    get_today_stats,
    get_current_driver,
    get_current_waiter,
)

# Importa os jobs de background do novo arquivo
from services.background_jobs import (
    sync_external_orders,
    run_opportunity_scanner,
    send_morning_reports,
    run_rfm_analysis_cron,
    dispatch_smart_event
)
from routers import (
    kds,
    auth,
    orders,
    menu,
    inventory,
    finance,
)  # Importa o novo arquivo de rotas

# --- Mantidos ---
from services.analytics import PizzaBrain
from services.crm_engine import run_crm_automations


from dotenv import load_dotenv
import re
import urllib.parse
from jose import JWTError, jwt


load_dotenv()

# Cria tabelas se não existirem
Base.metadata.create_all(bind=engine)

app = FastAPI(title="ALIV Growth Platform")

# Descobre onde o main.py está (ex: /root/pizzaria)
script_dir = os.path.dirname(os.path.abspath(__file__))

# Monta o caminho completo da pasta static (ex: /root/pizzaria/static)
static_path = os.path.join(script_dir, "static")

# Usa o caminho absoluto
app.mount("/static", StaticFiles(directory=static_path), name="static")

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(kds.router)
app.include_router(auth.router)  # <--- NOVO
app.include_router(orders.router)
app.include_router(menu.router)  # <--- NOVO
app.include_router(inventory.router)
app.include_router(finance.router)


# CONSTANTES
URL_EVENTOS_PERSONALIZADOS = "https://app.wabiz.delivery"


# --- MODELOS PYDANTIC ---
class WebEvent(BaseModel):
    event_name: str
    event_id: Optional[str] = None
    url: str
    user_agent: Optional[str] = None
    user_data: Optional[Dict[str, Any]] = None
    custom_data: Optional[Dict[str, Any]] = None
    pixel_id: Optional[str] = None


class AIRequest(BaseModel):
    start: str
    end: str
    payment_method: str


# ==========================================
#              ROTAS ADMIN (SaaS)
# ==========================================


# Em pizzaria/main.py

@app.get("/admin/dashboard", response_class=HTMLResponse)
def dashboard_view(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
    start: Optional[str] = None,
    end: Optional[str] = None,
    payment_method: Optional[str] = "Todos",
):
    # 1. Configurações de Fuso
    tz_br = pytz.timezone("America/Sao_Paulo")
    utc = pytz.utc
    now_br = datetime.now(tz_br)

    # 2. Define datas em Horário BRASIL (Início e Fim do dia)
    if start:
        try:
            start_date_br = datetime.strptime(start, "%Y-%m-%d")
        except:
            start_date_br = now_br.replace(tzinfo=None) - timedelta(days=30)
    else:
        start_date_br = now_br.replace(tzinfo=None) - timedelta(days=30)
        
    # Força hora 00:00 BRT
    start_dt_br = tz_br.localize(start_date_br.replace(hour=0, minute=0, second=0))

    if end:
        try:
            end_date_br = datetime.strptime(end, "%Y-%m-%d")
        except:
            end_date_br = now_br.replace(tzinfo=None)
    else:
        end_date_br = now_br.replace(tzinfo=None)
        
    # Força hora 23:59 BRT
    end_dt_br = tz_br.localize(end_date_br.replace(hour=23, minute=59, second=59))

    # Auto-correção de inversão
    if start_dt_br > end_dt_br:
        start_dt_br, end_dt_br = end_dt_br.replace(hour=0, minute=0), start_dt_br.replace(hour=23, minute=59)

    # 3. CONVERSÃO PARA UTC (AQUI ESTÁ A CORREÇÃO DOS RELATÓRIOS)
    # Transformamos "00:00 Brasil" em "03:00 UTC" para o banco entender certo
    start_utc = start_dt_br.astimezone(utc).replace(tzinfo=None)
    end_utc = end_dt_br.astimezone(utc).replace(tzinfo=None)

    # Inicializa o Cérebro
    brain = PizzaBrain(db, store_id=current_user.store_id)

    # Busca Dados (Usando UTC)
    kpis = brain.get_kpis(start_utc, end_utc, payment_method)
    top_products = brain.get_top_products(start_utc, end_utc, payment_method)
    heatmap_data = brain.get_sales_heatmap(start_utc, end_utc)
    customer_data = brain.get_customer_intelligence(90)
    today_stats = get_today_stats(db, current_user.store_id)

    # Prepara Gráficos
    chart_prod_labels = []
    chart_prod_data = []
    if top_products:
        chart_prod_labels = [p["name"][:15] + "..." for p in top_products]
        chart_prod_data = [p["qty"] for p in top_products]

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "current_user": current_user,
            "kpis": kpis,
            "heatmap": heatmap_data,
            "customers": customer_data,
            "today_stats": today_stats,
            "charts": {"prod_labels": chart_prod_labels, "prod_data": chart_prod_data},
            "filters": {
                # Devolve a data original (BR) para o HTML não ficar confuso
                "start": start_dt_br.strftime("%Y-%m-%d"),
                "end": end_dt_br.strftime("%Y-%m-%d"),
                "payment_method": payment_method,
            },
        },
    )


@app.get("/admin/events", response_class=HTMLResponse)
def admin_events(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
    start: Optional[str] = None,
    end: Optional[str] = None,
    event: Optional[str] = None,
    min_value: Optional[str] = None,
):
    query = db.query(Event).filter(Event.store_id == current_user.store_id)

    if start:
        try:
            query = query.filter(
                Event.created_at >= datetime.strptime(start, "%Y-%m-%d")
            )
        except:
            pass
    if end:
        try:
            query = query.filter(
                Event.created_at
                <= datetime.strptime(end, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59
                )
            )
        except:
            pass
    if event and event.strip():
        query = query.filter(Event.event_name.ilike(f"%{event.strip()}%"))
    if min_value and min_value.strip():
        try:
            val_float = float(min_value)
            query = query.filter(
                Event.custom_data["value"].astext.cast(Float) >= val_float
            )
        except:
            pass

    events = query.order_by(desc(Event.created_at)).limit(200).all()
    today_stats = get_today_stats(db, current_user.store_id)

    return templates.TemplateResponse(
        "events.html",
        {
            "request": request,
            "current_user": current_user,
            "events": events,
            "today_stats": today_stats,
            "filters": {
                "start": start or "",
                "end": end or "",
                "event": event or "",
                "min_value": min_value or "",
            },
        },
    )


# --- IA ROTAS ---
@app.post("/admin/ask-ai")
async def ask_ai_advisor(
    req: AIRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    brain = PizzaBrain(db, store_id=current_user.store_id)
    try:
        s = datetime.strptime(req.start, "%Y-%m-%d")
        e = datetime.strptime(req.end, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59
        )
    except:
        s = datetime.now() - timedelta(days=30)
        e = datetime.now()

    response = brain.ask_gemini_strategist(s, e, req.payment_method)
    return {"response": response}


@app.post("/admin/generate-creatives")
async def generate_creatives_advisor(
    req: AIRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    brain = PizzaBrain(db, store_id=current_user.store_id)
    try:
        s = datetime.strptime(req.start, "%Y-%m-%d")
        e = datetime.strptime(req.end, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59
        )
    except:
        s = datetime.now() - timedelta(days=30)
        e = datetime.now()

    script_ideas = brain.generate_creative_scripts(s, e)
    return {"response": script_ideas}


# ==========================================
#              ROTAS PÚBLICAS (API)
# ==========================================


@app.post("/api/track/web")
def track_web_event(
    event: WebEvent, 
    request: Request,
    db: Session = Depends(get_db)
):
    # 1. Validação Básica
    if "bot" in event.user_agent.lower() or "crawler" in event.user_agent.lower():
        return {"status": "ignored_bot"}

    # 2. Identifica a Loja
    store = db.query(Store).filter(Store.id == 1).first()
    if not store: store = db.query(Store).first()
        
    if not store: 
        print("🛑 [API Web] Nenhuma loja cadastrada no banco.")
        return {"status": "error_no_store"}

    # 3. Prepara Dados
    client_ip = request.client.host
    user_agent = event.user_agent
    user_data_in = event.user_data or {}
    
    user_data_unified = {
        "email": user_data_in.get("email"),
        "phone": user_data_in.get("phone"),
        "first_name": user_data_in.get("first_name"),
        "last_name": user_data_in.get("last_name"),
        "city": user_data_in.get("city"),
        "zip_code": user_data_in.get("zip_code"),
        "state": user_data_in.get("state"),
        "ip": client_ip,
        "user_agent": user_agent,
        "url": event.url,
        "fbp": user_data_in.get("fbp") or request.cookies.get("_fbp"),
        "fbc": user_data_in.get("fbc") or request.cookies.get("_fbc"),
        "external_id": user_data_in.get("external_id"),
        "gclid": user_data_in.get("gclid"),
        "session_id": user_data_in.get("session_id")
    }

    items = []
    if event.custom_data and "contents" in event.custom_data:
        items = event.custom_data["contents"]
    
    value = 0.0
    if event.custom_data:
        value = float(event.custom_data.get("value", 0))

    final_event_id = event.event_id
    if not final_event_id and event.custom_data:
        final_event_id = event.custom_data.get("transaction_id") or event.custom_data.get("order_id")

    # --- LÓGICA DE VALIDAÇÃO E FILTRAGEM ---
    targets_to_send = ["fb", "ga"]
    
    # Se for VENDA, exige validação no banco
    if event.event_name in ["Purchase", "Venda Real (Server) - Correta"]:
        
        if final_event_id:
            # 1. Tenta buscar no banco
            order_exists = db.query(Order).filter(
                Order.store_id == store.id,
                or_(
                    Order.wabiz_id == str(final_event_id),
                    Order.external_id == str(final_event_id),
                    cast(Order.id, String) == str(final_event_id)
                )
            ).first()
            
            if not order_exists:
                # EM VEZ DE ESPERAR, SALVAMOS PARA DEPOIS
                print(f"⏳ [API Web] Venda {final_event_id} ainda não existe. Salvando na fila de espera...")
                
                try:
                    from models import PendingPixelEvent
                    
                    # Evita duplicidade (se o cliente atualizar a página)
                    exists = db.query(PendingPixelEvent).filter(
                        PendingPixelEvent.event_id == str(final_event_id),
                        PendingPixelEvent.status == 'PENDING'
                    ).first()
                    
                    if not exists:
                        db.add(PendingPixelEvent(
                            store_id=store.id,
                            event_id=str(final_event_id),
                            event_name=event.event_name,
                            payload_json=event.dict(), # Salva GCLID, FBP, FBC, IP...
                            status="PENDING"
                        ))
                        db.commit()
                        
                    return {"status": "queued", "message": "Evento salvo. Aguardando sincronização do pedido."}
                    
                except Exception as e:
                    print(f"❌ Erro ao enfileirar evento: {e}")
                    return {"status": "error_queue"}
            
            # Se o pedido JÁ EXISTIR (raro, mas possível), o código continua abaixo e envia normal...
            print(f"✅ [API Web] Venda {final_event_id} já existe! Enviando agora.")
            targets_to_send = ["ga"]

    # Disparo Controlado
    pixel_results = dispatch_smart_event(
        store=store,
        event_name=event.event_name,
        user_data=user_data_unified,
        items=items,
        total_value=value,
        transaction_id=final_event_id or f"web_{int(time.time())}",
        targets=targets_to_send
    )

    # Log no Banco
    try:
        db_event = Event(
            store_id=store.id,
            event_name=event.event_name,
            event_id=final_event_id,
            url=event.url,
            user_agent=user_agent,
            client_ip=client_ip,
            user_data=event.user_data,
            custom_data=event.custom_data,
            sent_to_facebook=pixel_results.get("fb", False),
            sent_to_google=pixel_results.get("ga", False),
        )
        db.add(db_event)
        db.commit()
    except Exception as e:
        print(f"❌ Erro log DB: {e}")

    return {
        "status": "processed",
        "fb_sent": pixel_results.get("fb", False),
        "ga_sent": pixel_results.get("ga", False),
    }


@app.on_event("startup")
def start_scheduler():
    
    if os.getenv("RUN_SCHEDULER") != "true":
        print("⏰ [Scheduler] Ignorado neste worker (aguardando flag RUN_SCHEDULER=true).")
        return
    
    # Garante fuso horário do Brasil
    scheduler = BackgroundScheduler(timezone=pytz.timezone("America/Sao_Paulo"))

    # Jobs importados de services/background_jobs.py
    scheduler.add_job(sync_external_orders, "interval", seconds=30)
    scheduler.add_job(run_crm_automations, "cron", minute=0, args=[SessionLocal()])
    scheduler.add_job(run_opportunity_scanner, "interval", minutes=60)
    scheduler.add_job(send_morning_reports, "cron", hour=8, minute=0)
    scheduler.add_job(run_rfm_analysis_cron, "cron", hour=22, minute=35)

    scheduler.start()
    print("⏰ [Scheduler] Robôs iniciados (Refatorado).")


# ==========================================
#           GESTÃO DE LOJAS (SaaS)
# ==========================================


@app.get("/admin/stores", response_class=HTMLResponse)
def admin_stores_view(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    stores_db = db.query(Store).all()
    stores_list = []
    for s in stores_db:
        stores_list.append(
            {
                "id": s.id,
                "name": s.name,
                "slug": s.slug,
                "whatsapp_number": s.whatsapp_number,
                "is_open": s.is_open,
                "crm_schedule_hour": s.crm_schedule_hour,
                "address_name": s.address_name,
                "address_text": s.address_text,
                "address_lat": s.address_lat,
                "address_lng": s.address_lng,
                # Campos Antigos (Mantidos por compatibilidade visual se necessário)
                "integration_type": s.integration_type,
                "integration_url": s.integration_url,
                "integration_user": s.integration_user,
                "integration_password": s.integration_password,
                "fb_pixel_id": s.fb_pixel_id,
                "fb_access_token": s.fb_access_token,
                "ga4_measurement_id": s.ga4_measurement_id,
                "ga4_api_secret": s.ga4_api_secret,
                "whatsapp_api_token": s.whatsapp_api_token,
                "whatsapp_phone_id": s.whatsapp_phone_id,
                # --- CORREÇÃO: ENVIANDO O JSON NOVO ---
                "integrations_config": s.integrations_config or {},
            }
        )

    today_stats = get_today_stats(db, current_user.store_id)

    return templates.TemplateResponse(
        "stores.html",
        {
            "request": request,
            "stores": stores_list,
            "today_stats": today_stats,
            "current_user": current_user,
        },
    )


@app.post("/admin/stores/save")
async def save_store(
    store_id: Optional[int] = Form(None),
    name: str = Form(...),
    slug: str = Form(...),
    whatsapp: str = Form(None),
    is_open: bool = Form(True),
    crm_hour: int = Form(18),
    pixel_id: str = Form(None),
    access_token: str = Form(None),
    ga4_id: str = Form(None),
    ga4_secret: str = Form(None),
    wa_token: str = Form(None),
    wa_phone_id: str = Form(None),
    # Campos antigos (mantidos para compatibilidade ou pode remover se limpou o banco)
    int_type: str = Form("wabiz"),
    int_url: str = Form(None),
    int_user: str = Form(None),
    int_pass: str = Form(None),
    # --- NOVO CAMPO: JSON DE CONFIGURAÇÃO ---
    integrations_config_json: str = Form(None),
    # ----------------------------------------
    address_name: str = Form(None),
    address_text: str = Form(None),
    lat: str = Form(None),
    lng: str = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    try:
        # Processa o JSON recebido do formulário
        integrations_data = {}
        if integrations_config_json:
            try:
                integrations_data = json.loads(integrations_config_json)
            except Exception as e:
                print(f"⚠️ Erro ao decodificar JSON de integrações: {e}")

        data = {
            "name": name,
            "slug": slug,
            "whatsapp_number": whatsapp,
            "is_open": is_open,
            "crm_schedule_hour": crm_hour,
            "fb_pixel_id": pixel_id,
            "fb_access_token": access_token,
            "ga4_measurement_id": ga4_id,
            "ga4_api_secret": ga4_secret,
            "whatsapp_api_token": wa_token,
            "whatsapp_phone_id": wa_phone_id,
            # Mantém os campos antigos apenas para não quebrar tabelas legadas,
            # mas o sistema agora usará o 'integrations_config' abaixo
            "integration_type": int_type,
            "integration_url": int_url,
            "integration_user": int_user,
            "integration_password": int_pass,
            # --- SALVA O NOVO JSON ---
            "integrations_config": integrations_data,
            # -------------------------
            "address_name": address_name,
            "address_text": address_text,
            "address_lat": lat,
            "address_lng": lng,
        }

        if not store_id:
            store = Store(**data)
            db.add(store)
        else:
            store = db.query(Store).get(store_id)
            if not store:
                return JSONResponse(
                    status_code=404, content={"message": "Loja não encontrada"}
                )
            for key, value in data.items():
                setattr(store, key, value)

        db.commit()
        return {"success": True}
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"message": str(e)})


@app.delete("/admin/stores/{store_id}")
async def delete_store(
    store_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    if current_user.role != "owner":
        return JSONResponse(
            status_code=403, content={"message": "Apenas donos podem excluir lojas."}
        )

    store = db.query(Store).get(store_id)
    if not store:
        return JSONResponse(status_code=404, content={"message": "Loja não encontrada"})

    try:
        db.delete(store)
        db.commit()
        return {"success": True}
    except Exception as e:
        db.rollback()
        return JSONResponse(
            status_code=500,
            content={
                "message": "Não é possível excluir loja com pedidos/histórico. Tente desativá-la."
            },
        )


# Em pizzaria/main.py


# ==========================================
#           GESTÃO DE CAMPANHAS (CRM)
# ==========================================


@app.get("/admin/campaigns", response_class=HTMLResponse)
def admin_campaigns_view(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role(["owner", "manager"])),
):
    campaigns_db = (
        db.query(Campaign).filter(Campaign.store_id == current_user.store_id).all()
    )

    campaigns_list = []
    for c in campaigns_db:
        campaigns_list.append(
            {
                "id": c.id,
                "name": c.name,
                "trigger_type": c.trigger_type,
                "days_delay": c.days_delay,
                "message_template": c.message_template,
                "meta_template_name": c.meta_template_name,
                "is_active": c.is_active,
                "scheduled_at": c.scheduled_at.isoformat() if c.scheduled_at else None,
                "filter_rules": c.filter_rules or {},
            }
        )

    today_stats = get_today_stats(db, current_user.store_id)
    neigh_list = []
    try:
        from models import Address

        neighborhoods = (
            db.query(Address.neighborhood)
            .filter(
                Address.store_id == current_user.store_id,
                Address.neighborhood.isnot(None),
            )
            .distinct()
            .all()
        )
        neigh_list = sorted([n[0] for n in neighborhoods if n[0]])
    except:
        pass

    return templates.TemplateResponse(
        "campaigns.html",
        {
            "request": request,
            "campaigns": campaigns_list,
            "available_neighborhoods": neigh_list,
            "today_stats": today_stats,
            "current_user": current_user,
        },
    )


@app.post("/admin/campaigns/save")
async def save_campaign(
    camp_id: Optional[str] = Form(None),
    name: str = Form(...),
    trigger_type: str = Form(...),
    days_delay: Optional[str] = Form(None),
    message_template: str = Form(...),
    meta_template_name: str = Form(...),
    is_active: bool = Form(True),
    scheduled_at: str = Form(None),
    filter_neighborhoods: List[str] = Form([]),
    filter_min_spent: Optional[str] = Form(None),
    filter_last_order_days: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    try:
        final_camp_id = int(camp_id) if camp_id and camp_id.strip() else None
        final_days = int(days_delay) if days_delay and days_delay.strip() else 0
        final_min_spent = (
            float(filter_min_spent)
            if filter_min_spent and filter_min_spent.strip()
            else 0
        )
        final_last_days = (
            int(filter_last_order_days)
            if filter_last_order_days and filter_last_order_days.strip()
            else 0
        )

        dt_schedule = None
        if scheduled_at and trigger_type == "broadcast":
            try:
                dt_schedule = datetime.strptime(scheduled_at, "%Y-%m-%dT%H:%M")
            except ValueError:
                try:
                    dt_schedule = datetime.strptime(scheduled_at, "%Y-%m-%dT%H:%M:%S")
                except:
                    pass

        filter_rules = {
            "neighborhoods": filter_neighborhoods,
            "min_spent": final_min_spent,
            "last_order_days": final_last_days,
        }

        if not final_camp_id:
            camp = Campaign(
                store_id=current_user.store_id,
                name=name,
                trigger_type=trigger_type,
                days_delay=final_days,
                message_template=message_template,
                meta_template_name=meta_template_name,
                is_active=is_active,
                scheduled_at=dt_schedule,
                filter_rules=filter_rules,
            )
            db.add(camp)
        else:
            camp = db.query(Campaign).get(final_camp_id)
            if not camp or camp.store_id != current_user.store_id:
                return JSONResponse(
                    status_code=403, content={"message": "Sem permissão"}
                )

            camp.name = name
            camp.trigger_type = trigger_type
            camp.days_delay = final_days
            camp.message_template = message_template
            camp.meta_template_name = meta_template_name
            camp.is_active = is_active
            camp.scheduled_at = dt_schedule
            camp.filter_rules = filter_rules

        db.commit()
        return {"success": True}

    except Exception as e:
        db.rollback()
        print(f"❌ Erro ao salvar campanha: {e}")
        return JSONResponse(status_code=500, content={"message": str(e)})


@app.delete("/admin/campaigns/{camp_id}")
async def delete_campaign(
    camp_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    camp = db.query(Campaign).get(camp_id)
    if not camp or camp.store_id != current_user.store_id:
        return JSONResponse(status_code=403, content={"message": "Erro"})

    db.delete(camp)
    db.commit()
    return {"success": True}


# Rota para a IA gerar a campanha
@app.post("/admin/campaigns/ai-generate")
async def generate_campaign_ai(
    goal: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    brain = PizzaBrain(db, current_user.store_id)
    blueprint = brain.generate_campaign_blueprint(goal)
    return blueprint


# --- API PARA O FRONTEND ---


@app.get("/admin/insights")
def get_insights(
    db: Session = Depends(get_db), current_user: User = Depends(check_db_auth)
):
    insights = (
        db.query(Insight)
        .filter(Insight.store_id == current_user.store_id, Insight.is_archived == False)
        .order_by(desc(Insight.created_at))
        .all()
    )
    return insights


@app.post("/admin/insights/{insight_id}/archive")
def archive_insight(
    insight_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    ins = db.query(Insight).get(insight_id)
    if ins and ins.store_id == current_user.store_id:
        ins.is_archived = True
        db.commit()
    return {"success": True}


# ==========================================
#           GESTÃO DE CLIENTES (CRM)
# ==========================================


# --- ATUALIZAÇÃO: Rota de Visualização de Clientes com Novos Filtros ---
@app.get("/admin/customers", response_class=HTMLResponse)
def admin_customers_view(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
    search: Optional[str] = None,
    segment: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    # 1. Query Base
    query = db.query(Customer).filter(Customer.store_id == current_user.store_id)

    # 2. Filtro de Busca (Texto)
    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Customer.name.ilike(term),
                Customer.phone.ilike(term),
                Customer.email.ilike(term),
            )
        )

    # 3. Filtro de Segmento RFM
    # 3. Filtro de Segmento (RFM ou Débito)
    if segment == "Debito":
        # Busca clientes que tenham pelo menos um pedido "Fiado" não cancelado
        # Faz o join pelo telefone (elo de ligação)
        query = query.filter(
            db.query(Order.id)
            .filter(
                Order.store_id == current_user.store_id,
                Order.customer_phone == Customer.phone,  # Vínculo
                Order.payment_method.ilike("%Fiado%"),
                not_(Order.status.ilike("%CANCELADO%")),
            )
            .exists()
        )
    elif segment and segment != "Todos":
        query = query.filter(Customer.rfm_segment.ilike(f"%{segment}%"))

    # 4. Filtro de Data (Corrigido e Mais Robusto)
    if start_date and start_date.strip():
        try:
            # Converte e força inicio do dia
            dt_start = datetime.strptime(start_date, "%Y-%m-%d").replace(
                hour=0, minute=0, second=0
            )
            query = query.filter(Customer.last_order_at >= dt_start)
        except ValueError:
            print(f"⚠️ Erro de formato na Data Inicio: {start_date}")

    if end_date and end_date.strip():
        try:
            # Converte e força final do dia
            dt_end = datetime.strptime(end_date, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59
            )
            query = query.filter(Customer.last_order_at <= dt_end)
        except ValueError:
            print(f"⚠️ Erro de formato na Data Fim: {end_date}")

    customers_db = query.order_by(desc(Customer.last_order_at)).limit(100).all()

    # 5. Serialização
    customers_list = []
    for c in customers_db:
        last_addr = None
        if c.addresses:
            last_addr_obj = sorted(
                c.addresses, key=lambda x: x.last_used_at or datetime.min, reverse=True
            )[0]
            last_addr = {
                "street": last_addr_obj.street,
                "number": last_addr_obj.number,
                "neighborhood": last_addr_obj.neighborhood,
                "city": last_addr_obj.city,
                "state": last_addr_obj.state,
                "zip_code": last_addr_obj.zip_code,
                "complement": last_addr_obj.complement,
            }

        birth_fmt = c.birth_date.strftime("%Y-%m-%d") if c.birth_date else ""
        last_order_fmt = (
            c.last_order_at.strftime("%d/%m/%Y") if c.last_order_at else "-"
        )

        customers_list.append(
            {
                "id": c.id,
                "name": c.name,
                "phone": c.phone,
                "email": c.email or "",
                "birth_date_iso": birth_fmt,
                "last_order_fmt": last_order_fmt,
                "total_spent": c.total_spent,
                "address": last_addr,
                "rfm_segment": c.rfm_segment or "Novato",
            }
        )

    today_stats = get_today_stats(db, current_user.store_id)

    return templates.TemplateResponse(
        "customers.html",
        {
            "request": request,
            "customers": customers_list,
            "today_stats": today_stats,
            "current_user": current_user,
            "filters": {
                "search": search or "",
                "segment": segment or "Todos",
                "start": start_date or "",
                "end": end_date or "",
            },
        },
    )


@app.post("/admin/customers/save")
async def save_customer(
    cust_id: Optional[int] = Form(None),
    name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(None),
    birth_date: str = Form(None),
    street: str = Form(None),
    number: str = Form(None),
    neighborhood: str = Form(None),
    city: str = Form(None),
    state: str = Form(None),
    zip_code: str = Form(None),
    complement: str = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    try:
        clean_phone = "".join(filter(str.isdigit, phone))
        if len(clean_phone) in [10, 11]:
            clean_phone = "55" + clean_phone

        dt_birth = None
        if birth_date:
            try:
                dt_birth = datetime.strptime(birth_date, "%Y-%m-%d")
            except:
                pass

        if not cust_id:
            exists = (
                db.query(Customer)
                .filter(
                    Customer.store_id == current_user.store_id,
                    Customer.phone == clean_phone,
                )
                .first()
            )

            if exists:
                return JSONResponse(
                    status_code=400,
                    content={"message": "Já existe um cliente com este telefone."},
                )

            cust = Customer(
                store_id=current_user.store_id,
                name=name,
                phone=clean_phone,
                email=email,
                birth_date=dt_birth,
                last_order_at=datetime.now(),
            )
            db.add(cust)
            db.commit()
            db.refresh(cust)
        else:
            cust = db.query(Customer).get(cust_id)
            if not cust or cust.store_id != current_user.store_id:
                return JSONResponse(
                    status_code=403, content={"message": "Sem permissão."}
                )

            cust.name = name
            cust.phone = clean_phone
            cust.email = email
            cust.birth_date = dt_birth
            db.commit()

        if street and neighborhood:
            addr_exists = (
                db.query(Address)
                .filter(
                    Address.customer_id == cust.id,
                    Address.street == street,
                    Address.number == number,
                    Address.zip_code == zip_code,
                )
                .first()
            )

            if not addr_exists:
                # Endereço novo: Cria do zero
                new_addr = Address(
                    customer_id=cust.id,
                    store_id=current_user.store_id,
                    street=street,
                    number=number,
                    neighborhood=neighborhood,
                    city=city,
                    state=state,
                    zip_code=zip_code,
                    complement=complement,
                    last_used_at=datetime.now(),
                )
                db.add(new_addr)
            else:
                # Endereço existente: ATUALIZA OS CAMPOS (Correção do Bug)
                addr_exists.neighborhood = (
                    neighborhood  # <--- Força a atualização do bairro
                )
                addr_exists.complement = complement  # <--- Atualiza complemento
                addr_exists.city = city
                addr_exists.state = state
                addr_exists.last_used_at = datetime.now()

            db.commit()

        return {"success": True}

    except Exception as e:
        db.rollback()
        print(f"Erro ao salvar cliente: {e}")
        return JSONResponse(
            status_code=500, content={"message": "Erro interno ao salvar dados."}
        )


@app.delete("/admin/customers/{cust_id}")
async def delete_customer(
    cust_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    if current_user.role not in ["owner", "manager"]:
        return JSONResponse(status_code=403, content={"message": "Sem permissão."})

    cust = db.query(Customer).get(cust_id)

    if not cust or cust.store_id != current_user.store_id:
        return JSONResponse(
            status_code=404, content={"message": "Cliente não encontrado."}
        )

    try:
        db.query(Address).filter(Address.customer_id == cust.id).delete()
        db.delete(cust)
        db.commit()
        return {"success": True}

    except Exception as e:
        db.rollback()
        return JSONResponse(
            status_code=500,
            content={
                "message": "Não é possível excluir cliente com histórico de compras (Pedidos)."
            },
        )


# --- NOVA API: Histórico de Pedidos do Cliente (Para o Modal) ---
@app.get("/admin/api/customer/{cust_id}/orders")
def get_customer_orders_api(
    cust_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    # Verifica se o cliente pertence à loja do usuário
    customer = db.query(Customer).get(cust_id)
    if not customer or customer.store_id != current_user.store_id:
        return JSONResponse(
            status_code=404, content={"error": "Cliente não encontrado"}
        )

    # Busca pedidos usando o telefone do cliente (elo de ligação)
    if not customer.phone:
        return []

    orders = (
        db.query(Order)
        .filter(
            Order.store_id == current_user.store_id,
            Order.customer_phone == customer.phone,
        )
        .order_by(desc(Order.created_at))
        .limit(20)
        .all()
    )  # Limite de 20 para não pesar

    history = []
    for o in orders:
        history.append(
            {
                "id": o.wabiz_id,
                "date": o.created_at.strftime("%d/%m/%Y %H:%M"),
                "total": o.total_value,
                "payment": o.payment_method,
                "items": o.items_json or [],
            }
        )

    return history


# --- ROTA DE MANUTENÇÃO (REPARO INTELIGENTE) ---
@app.get("/admin/maintenance/fix-ltv")
def fix_ltv_database(
    db: Session = Depends(get_db), current_user: User = Depends(check_role(["owner"]))
):
    """
    Recalcula LTV e Datas procurando por variações de telefone (Com/Sem 55).
    Também dispara um teste para o Celery ver se ele está vivo.
    """

    customers = db.query(Customer).all()
    updated_count = 0

    for cust in customers:
        if not cust.phone:
            continue

        # Lógica Detetive: Cria variações do telefone
        clean_phone = re.sub(r"\D", "", cust.phone)
        variations = [clean_phone]
        if clean_phone.startswith("55"):
            variations.append(clean_phone[2:])  # Sem 55
        else:
            variations.append(f"55{clean_phone}")  # Com 55

        # Busca estatísticas reais na tabela de Pedidos usando IN
        stats = (
            db.query(
                func.sum(Order.total_value).label("total"),
                func.max(Order.created_at).label("last_date"),
                func.count(Order.id).label("qtd"),
            )
            .filter(
                Order.store_id == cust.store_id,
                Order.customer_phone.in_(variations),  # <--- O PULO DO GATO
            )
            .first()
        )

        total_real = stats.total or 0.0
        last_date_real = stats.last_date
        count_real = stats.qtd or 0

        changed = False

        # Correção LTV
        if abs((cust.total_spent or 0.0) - total_real) > 0.01:
            cust.total_spent = total_real
            changed = True

        # Correção Data
        if last_date_real and cust.last_order_at != last_date_real:
            cust.last_order_at = last_date_real
            changed = True

        # Correção Contagem
        if cust.order_count != count_real:
            cust.order_count = count_real
            changed = True

        if changed:
            updated_count += 1

    db.commit()

    # AGORA SIM: Dispara o Celery para recalcular a RFM em background
    # Olhe o log do Celery depois de rodar isso!
    task_run_rfm_analysis.delay(current_user.store_id)

    return {
        "message": f"🔧 Reparo Inteligente: {updated_count} clientes corrigidos.",
        "celery": "🚀 Tarefa enviada para o Celery! Verifique os logs do worker.",
    }


# --- FUNÇÃO AUXILIAR (MOVIDA PARA FORA DA ROTA) ---
def prepare_driver_items(items_raw):
    if not items_raw: return [] # Proteção contra None
    processed = []
    for item in items_raw:
        raw_title = item.get('title', '')
        family_name = None
        clean_name = raw_title

        # A) SE FOR HTML (Regex mais seguro)
        if '<div' in raw_title or '<span' in raw_title:
            # Tenta pegar família, se falhar, segue o jogo
            try:
                html_match = re.search(r'(?:text-yellow|tracking-widest)[^>]*>([^<]+)</div>', raw_title)
                if html_match: family_name = html_match.group(1).strip()
            except: pass
            
            # Limpeza de HTML agressiva mas segura
            temp_text = raw_title.replace('</div>', ' ').replace('<br>', ' + ')
            clean_name = re.sub(r'<[^>]+>', '', temp_text)
            clean_name = re.sub(r'\s+', ' ', clean_name).strip()
            
            if family_name and clean_name.startswith(family_name):
                clean_name = clean_name[len(family_name):].strip()

        # B) SE FOR TEXTO COM SEPARADOR
        elif ':' in raw_title:
            parts = raw_title.split(':', 1)
            family_name = parts[0].strip()
            clean_name = parts[1].strip()
        elif ' - ' in raw_title:
            parts = raw_title.rsplit(' - ', 1)
            clean_name = parts[0].strip()
            family_name = parts[1].strip()

        if not family_name and 'groupName' in item:
            family_name = item['groupName']

        # Classificação (Mantida a sua lógica original, que é boa)
        upper_name = clean_name.upper()
        is_drink = any(k in upper_name for k in ['REFRI', 'COCA', 'GUARAN', 'SUCO', 'CERVEJA', 'AGUA', 'H2OH', 'LATA', '2L', '600ML', 'PEPSI', 'FANTA', 'SPRITE'])
        is_esfiha = "ESFIHA" in upper_name or (family_name and "ESFIHA" in family_name.upper())
        is_pizza = not is_drink and not is_esfiha
        
        details_list = []
        
        # 1. Tenta pegar de 'details' (Formato Novo do Normalizer)
        if item.get('details') and isinstance(item['details'], list):
            for det in item['details']:
                txt = det.get('text') if isinstance(det, dict) else str(det)
                # Limpa ícones visuais
                txt = txt.replace('•', '').replace('+', '').strip()
                if txt: details_list.append(txt)
                
        # 2. Fallback: Tenta pegar de 'parts' (Formato Antigo)
        elif item.get('parts') and isinstance(item['parts'], list):
            # Filtra nomes de partes que sejam diferentes do nome principal para não repetir
            parts_clean = [str(p) for p in item['parts'] if str(p) != clean_name]
            details_list.extend(parts_clean)
            
        # 3. Adiciona Observação do Item
        if item.get('observation'):
            details_list.append(f"Obs: {item['observation']}")
            
        final_desc = " + ".join(details_list)
        

        processed.append({
            "qty": item.get('quantity', 1),
            "family": family_name, 
            "name": clean_name,    
            "details_str": final_desc,
            "is_pizza": is_pizza,
            "is_esfiha": is_esfiha,
            "is_drink": is_drink
        })
    return processed


@app.get("/driver/app", response_class=HTMLResponse)
def driver_app_view(request: Request, db: Session = Depends(get_db)):
    # 1. Recupera Token
    token = request.cookies.get("driver_token")
    if not token:
        return RedirectResponse(url="/driver/login", status_code=303)

    # 2. Verifica Token
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise ValueError("Token sem usuário")
    except Exception as e:
        return RedirectResponse(url="/driver/login", status_code=303)

    # 3. Busca Motorista
    driver = db.query(User).filter(User.email == username).first()
    if not driver or driver.role != "driver":
        return RedirectResponse(url="/driver/login", status_code=303)

    # 4. Busca Pedidos (SAIU_ENTREGA) - Lista Visual
    orders = (
        db.query(Order)
        .filter(Order.driver_id == driver.id, Order.status == "SAIU_ENTREGA")
        .all()
    )

    # Injeta dados visuais (itens)
    for order in orders:
        if order.items_json:
            try:
                order.driver_items_view = prepare_driver_items(order.items_json)
            except:
                order.driver_items_view = []
        else:
            order.driver_items_view = []
            
        # 1. Observação do Pedido
        order.driver_notes_view = order.notes 

        # 2. Extração e Limpeza do Troco
        order.driver_change_view = None
        # Cria uma versão limpa do pagamento para exibir (Padrão = original)
        order.driver_payment_view = order.payment_method 

        if order.payment_method and "Troco" in order.payment_method:
            # A. Tenta extrair o valor para a caixa amarela
            match = re.search(r'->\s*R\$\s*([\d\.,]+)', order.payment_method)
            if match:
                order.driver_change_view = match.group(1)
            
            # B. Limpa o texto principal (Remove tudo a partir do ' (Troco')
            # Ex: "Dinheiro (Troco p/ 20...)" vira apenas "Dinheiro"
            if "(" in order.payment_method:
                order.driver_payment_view = order.payment_method.split('(')[0].strip()
                
        # --------------------------------

    # 5. CÁLCULO DA SESSÃO E HISTÓRICO
    active_session = (
        db.query(DriverSession)
        .filter(DriverSession.driver_id == driver.id, DriverSession.end_time == None)
        .first()
    )
    
    session_stats = {"fees": 0.0, "count": 0}
    history_list = [] 
    
    if active_session:
        # A. Pedidos Entregues (Ganhos do Turno Atual)
        delivered_orders = (
            db.query(Order)
            .filter(
                Order.driver_id == driver.id,
                Order.status.in_(['ENTREGUE', 'CONCLUIDO']), 
                Order.created_at >= active_session.start_time,
            )
            .order_by(desc(Order.created_at))
            .all()
        )
        
        # B. Movimentações Financeiras (CORREÇÃO: Pega TODAS as pendências, inclusive Dívidas Antigas)
        all_advances = db.query(DriverAdvance).join(DriverSession).filter(
            DriverSession.driver_id == driver.id,
            DriverAdvance.is_paid == False
        ).all()
        
        # C. Separação Créditos vs Débitos
        # Negativos = Crédito (Bônus, Diária, Resto a Receber)
        total_credits = sum(abs(a.amount) for a in all_advances if a.amount < 0)
        
        # Positivos = Débito (Vales, Resto a Pagar/Dívida)
        total_debits = sum(a.amount for a in all_advances if a.amount > 0)
        
        # D. Cálculo Final
        sum_fees = sum(o.delivery_fee or 0.0 for o in delivered_orders)
        sum_tips = sum(o.driver_tip or 0.0 for o in delivered_orders)
        
        # Bruto = O que ele "fez" (Entregas + Gorjetas + Créditos)
        gross_earnings = sum_fees + sum_tips + total_credits
        
        # Líquido = Bruto - O que ele deve (Vales + Dívida Antiga)
        net_balance = gross_earnings - total_debits
        
        session_stats["count"] = len(delivered_orders)
        
        # Passa os valores separados para o template
        session_stats["gross"] = gross_earnings
        session_stats["debits"] = total_debits
        session_stats["net"] = net_balance
        session_stats["fees"] = net_balance # Mantém compatibilidade
        
        # --- INÍCIO DA RESTAURAÇÃO DO HISTÓRICO ---
        tz_br = pytz.timezone('America/Sao_Paulo')
        utc = pytz.utc
        
        for o in delivered_orders:
            # Formatação de Hora
            raw_dt = o.created_at
            if raw_dt.tzinfo is None: raw_dt = utc.localize(raw_dt)
            local_dt = raw_dt.astimezone(tz_br)
            
            street = o.address_street or "Retirada"
            number = o.address_number or "S/N"
            neighborhood = o.address_neighborhood or ""
            
            addr_short = f"{street}, {number}"
            if neighborhood:
                addr_short += f" - {neighborhood}"
            
            history_list.append({
                "id": o.wabiz_id or o.id,
                "time": local_dt.strftime('%H:%M'),
                "address": addr_short,
                "fee": o.delivery_fee or 0.0
            })

    # 6. GERAÇÃO DE ROTA
    orders.sort(key=lambda x: (x.delivery_fee or 0, x.address_neighborhood or ""))
    maps_url = "#"
    if orders:
        def clean_addr(o):
            st = (o.address_street or "").strip()
            num = str(o.address_number or "S/N").strip()
            nei = (o.address_neighborhood or "").strip()
            city = o.address_city or ""
            return f"{st}, {num} - {nei}, {city}"

        origin_encoded = urllib.parse.quote(driver.store.address_text or "")
        destinations = [urllib.parse.quote(clean_addr(o)) for o in orders]
        if destinations:
            final = destinations.pop()
            waypoints = f"&waypoints={'%7C'.join(destinations)}" if destinations else ""
            maps_url = f"https://www.google.com/maps/dir/?api=1&origin={origin_encoded}&destination={final}{waypoints}&travelmode=driving"

    return templates.TemplateResponse(
        "driver_app.html",
        {
            "request": request,
            "orders": orders,
            "count": len(orders),
            "maps_url": maps_url,
            "driver": driver,
            "session": session_stats,
            "history": history_list,
            "is_online": True,
            "access_token": token,
        },
    )


# 4. ROTA DE BAIXA (MANTIDA)
@app.post("/driver/complete/{order_id}")
async def complete_delivery(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_driver),
):
    if not current_user:
        return JSONResponse(status_code=401, content={"message": "Não autorizado"})

    order = (
        db.query(Order)
        .filter(Order.id == order_id, Order.driver_id == current_user.id)
        .first()
    )
    if order:
        order.status = "ENTREGUE"
        db.commit()

        await manager.broadcast(order.store_id, "update")

        return {"success": True}
    return JSONResponse(status_code=400, content={"message": "Erro ao finalizar"})


# --- CONTROLE DE TURNO (SESSÃO) ---


@app.post("/driver/session/start")
async def start_driver_session(
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(
        get_current_driver
    ),  # Usa Optional para evitar erro 500 se não logado
):
    if not current_user:
        return JSONResponse(status_code=401, content={"message": "Auth necessária"})

    # Verifica se já tem turno aberto
    active = (
        db.query(DriverSession)
        .filter(
            DriverSession.driver_id == current_user.id, DriverSession.end_time == None
        )
        .first()
    )

    if not active:
        session = DriverSession(
            driver_id=current_user.id, store_id=current_user.store_id
        )
        db.add(session)
        db.commit()

    return {"success": True}


@app.post("/driver/session/stop")
async def stop_driver_session(
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_driver),
):
    if not current_user:
        return JSONResponse(status_code=401, content={"message": "Auth necessária"})

    session = (
        db.query(DriverSession)
        .filter(
            DriverSession.driver_id == current_user.id, DriverSession.end_time == None
        )
        .first()
    )

    if session:
        # Calcula totais da noite para fechar o caixa
        orders = (
            db.query(Order)
            .filter(
                Order.driver_id == current_user.id,
                Order.status == "ENTREGUE",
                Order.created_at >= session.start_time,
            )
            .all()
        )

        count = len(orders)
        total_fees = 0.0

        # Soma as taxas de entrega
        for o in orders:
            if o.items_json:
                for item in o.items_json:
                    if item.get("title") == "Taxa de Entrega":
                        try:
                            total_fees += float(item.get("price", 0))
                        except:
                            pass

        session.end_time = datetime.now()
        session.total_deliveries = count
        session.total_amount_due = total_fees
        db.commit()

        return {"success": True, "summary": {"count": count, "total": total_fees}}

    return {"success": False, "message": "Nenhum turno aberto."}


# ==========================================
#           APP DO GARÇOM (MESA)
# ==========================================


# 3. APLICAÇÃO PRINCIPAL (SPA)
@app.get("/waiter/app", response_class=HTMLResponse)
def waiter_app_view(request: Request, current_user: User = Depends(get_current_waiter)):
    if not current_user:
        return RedirectResponse(url="/waiter/login")
    return templates.TemplateResponse(
        "waiter_app.html", {"request": request, "user": current_user}
    )


@app.get("/admin/maintenance/add-columns-v2")
def add_columns_v2():
    from sqlalchemy import text

    with engine.connect() as conn:
        try:
            conn.execute(
                text(
                    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS driver_tip FLOAT DEFAULT 0.0;"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_credit FLOAT DEFAULT 0.0;"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_fee FLOAT DEFAULT 0.0;"
                )
            )
            conn.commit()
            return {
                "status": "success",
                "message": "Colunas adicionadas (Tip, Credit, Fee).",
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}


# --- ROTA DE MANUTENÇÃO DO SISTEMA (BOTÃO OTIMIZAR) ---
@app.post("/admin/system/optimize")
def optimize_system(
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role(["owner", "manager"])) # Apenas Gerentes/Donos
):
    from sqlalchemy import text
    import time
    
    start_time = time.time()
    log_msgs = []

    try:
        # 1. Cria Índices (Garante estrutura)
        commands = [
            "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);",
            "CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders (created_at);",
            "CREATE INDEX IF NOT EXISTS idx_orders_store_status ON orders (store_id, status);",
            "CREATE INDEX IF NOT EXISTS idx_orders_customer_phone ON orders (customer_phone);"
        ]
        
        # Usa conexão direta para comandos DDL
        with engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            for sql in commands:
                conn.execute(text(sql))
            
            # 2. Otimização Profunda (Vacuum Analyze)
            # Isso limpa "espaços mortos" no banco e atualiza estatísticas de velocidade
            conn.execute(text("VACUUM ANALYZE;"))
            log_msgs.append("Banco de dados otimizado e índices verificados.")

        # 3. Limpeza de Memória (Python)
        import gc
        gc.collect()
        log_msgs.append("Memória do servidor limpa.")

        elapsed = time.time() - start_time
        return {
            "success": True, 
            "message": f"Sistema Otimizado em {elapsed:.2f}s! 🚀", 
            "details": log_msgs
        }

    except Exception as e:
        print(f"❌ Erro na otimização: {e}")
        return JSONResponse(status_code=500, content={"message": f"Erro: {str(e)}"})
