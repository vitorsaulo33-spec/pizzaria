from sqlalchemy.orm import Session
from datetime import datetime
from sqlalchemy import desc
from models import Order, Store, Insight, DeliveryFee
from services.integrations.wabiz import WabizAdapter
from services.integrations.ifood import IfoodAdapter
from services.analytics import PizzaBrain
from services.stock_engine import auto_learn_product, deduct_stock_from_order, enrich_order_with_combo_data
from services.utils import recover_historical_ip, upsert_customer_smart, upsert_address, dispatch_smart_event, get_active_cash_id
from services.whatsapp import send_whatsapp_template
from services.tasks import task_send_whatsapp, task_run_rfm_analysis
from database import SessionLocal
import requests
import json
from models import PendingPixelEvent

# ==========================================
#        FACTORY DE INTEGRAÇÕES (O HUB)
# ==========================================

def get_active_adapters(store: Store):
    """
    Lê a configuração JSON (integrations_config) e retorna
    uma lista com TODOS os adaptadores ativos (Wabiz, iFood, etc).
    """
    adapters = []
    
    # Lê o campo JSON do banco (ou dicionário vazio se for nulo)
    config = store.integrations_config or {}
    
    # 1. Configuração Wabiz
    wz = config.get('wabiz', {})
    if wz.get('active') and wz.get('user'):
        try:
            adapters.append(WabizAdapter(
                token=None,
                user=wz.get('user'),
                password=wz.get('pass'),
                base_url=wz.get('url') or "https://delivery.wabiz.com.br/api/v1"
            ))
        except Exception as e:
            print(f"⚠️ Erro ao iniciar Wabiz: {e}")

    # 2. Configuração iFood
    ifood = config.get('ifood', {})
    if ifood.get('active') and ifood.get('client_id'):
        try:
            adapters.append(IfoodAdapter(
                user=ifood.get('client_id'),
                password=ifood.get('client_secret'),
                merchant_id=ifood.get('merchant_id')
            ))
        except Exception as e:
            print(f"⚠️ Erro ao iniciar iFood: {e}")
        
    return adapters

# ==========================================
#    PROCESSADOR DE PEDIDOS PADRONIZADOS
# ==========================================
def process_standard_order(db: Session, store: Store, order_data: dict):
    """
    Processa um pedido padronizado vindo de qualquer integração (Hub Universal).
    VERSÃO FINAL: Taxa Inteligente + Confirmação Auto + Logs.
    """
    try:
        ext_id = order_data['external_id']
        
        # 1. VERIFICA SE É UMA ATUALIZAÇÃO (Ex: Cancelamento)
        if order_data.get('is_update'):
            existing = db.query(Order).filter(Order.store_id == store.id, Order.external_id == ext_id).first()
            if existing and order_data.get('status') == 'CANCELADO':
                print(f"🚫 [Hub] Cancelamento recebido iFood #{ext_id}")
                existing.status = f"CANCELADO ({order_data.get('notes')})"
                
                # Estorna estoque
                try:
                    from services.stock_engine import return_stock_from_order
                    if existing.items_json:
                        return_stock_from_order(db, store.id, existing.items_json, "ifood-cancel")
                except Exception as e:
                    print(f"⚠️ [Hub] Erro ao estornar estoque: {e}")
                
                db.commit()
            return
        
        # Evita duplicidade
        if db.query(Order).filter(Order.store_id == store.id, Order.external_id == ext_id).first():
            return
        if db.query(Order).filter(Order.store_id == store.id, Order.wabiz_id == ext_id).first():
            return

        display_id = order_data.get('display_id') or ext_id
        print(f"📦 [Hub] Processando Pedido #{display_id}...")
        
        cust = order_data['customer']
        addr = order_data['address']
        items = order_data['items']
        
        # Valores iniciais
        total = float(order_data['total_value'])
        delivery_fee = float(order_data.get('delivery_fee', 0.0))
        delivery_type = order_data.get('delivery_type', 'delivery')
        discount = float(order_data.get('discount', 0.0))

        # --- CORREÇÃO DE TAXA INTELIGENTE ---
        neighborhood = str(addr.get('neighborhood', '')).strip().upper()
        
        if delivery_type == 'delivery' and delivery_fee == 0.0 and neighborhood:
            try:
                fee_obj = db.query(DeliveryFee).filter(
                    DeliveryFee.store_id == store.id,
                    DeliveryFee.neighborhood == neighborhood
                ).first()
                
                if fee_obj:
                    print(f"💰 [Hub] Taxa recuperada pelo bairro '{neighborhood}': R$ {fee_obj.fee}")
                    delivery_fee = fee_obj.fee
                    total += delivery_fee # Ajusta o total
            except Exception as e:
                print(f"⚠️ [Hub] Falha ao recuperar taxa: {e}")
        # ------------------------------------
        
        integration_source = "manual"

        # 1. CRM
        customer_db = None
        if cust.get('phone'):
            try:
                customer_db = upsert_customer_smart(db, store.id, cust['phone'], cust['name'], cust['email'], total)
                if customer_db and addr.get('street'):
                    upsert_address(db, customer_db.id, store.id, addr)
            except Exception as e: print(f"⚠️ Erro CRM: {e}")

        # 2. Estoque
        for item in items:
            try: auto_learn_product(db, store.id, item, integration_source)
            except: pass

        try: deduct_stock_from_order(db, store.id, items, integration_source)
        except Exception as e: print(f"⚠️ Erro Estoque: {e}")
        
        # --- ENRIQUECIMENTO AUTOMÁTICO (INTEGRAÇÕES) ---
        # Se o iFood mandou "Combo Galera" sem lista, nós preenchemos aqui
        enrich_order_with_combo_data(db, store.id, items)
        # -----------------------------------------------
        
        # 3. Pixel (Purchase)
        pixel_results = {"fb": False, "ga": False}
        try:
            client_ip = recover_historical_ip(db, store.id, cust.get('phone'), cust.get('email'))
            
            # Melhora o Match Quality separando Nome e Sobrenome
            full_name = cust.get('name', '') or ""
            first_name = full_name.split()[0] if full_name else ""
            last_name = " ".join(full_name.split()[1:]) if full_name and len(full_name.split()) > 1 else ""

            user_data_pixel = {
                "email": cust.get('email'), 
                "phone": cust.get('phone'), 
                "first_name": first_name,
                "last_name": last_name,
                "city": addr.get('city', 'Itanhaem'), 
                "zip_code": addr.get('zip_code', '11740000'),
                "state": addr.get('state', 'SP'),
                "ip": client_ip, 
                "user_agent": "AlivHub/Server-Side", 
                
                # --- CORREÇÃO SEGURA ---
                # Usa o ext_id (já definido no início da função) como fallback final
                "external_id": cust.get('email') or cust.get('phone') or str(ext_id),
                # -----------------------
                
                "url": "https://app.wabiz.delivery/pedido_confirmado" 
            }
            
            # ESTRATÉGIA HÍBRIDA:
            # targets=["fb"] -> Envia APENAS para o Facebook via Servidor.
            # (O Google será enviado pelo GTM no navegador para garantir atribuição correta)
            pixel_results = dispatch_smart_event(
                store, 
                "Venda Real (Server) - Correta", 
                user_data_pixel, 
                items, 
                total, 
                ext_id, 
                targets=["fb"] # <--- AQUI ESTÁ A CHAVE DA MUDANÇA
            )
        except Exception as e: 
            print(f"❌ Erro Pixel: {e}")

        # 4. Salvamento
        try: items_safe = json.loads(json.dumps(items, default=str))
        except: items_safe = items

        active_cash_id = get_active_cash_id(db, store.id)

        new_order = Order(
            store_id=store.id, 
            wabiz_id=display_id,
            external_id=ext_id,
            status="PREPARO",
            customer_name=cust.get('name', 'Cliente'), 
            customer_phone=cust.get('phone'), 
            customer_email=cust.get('email'),
            address_street=addr.get('street'), 
            address_number=addr.get('number'),
            address_neighborhood=addr.get('neighborhood'), 
            address_city=addr.get('city'),
            address_state=addr.get('state'), 
            total_value=total,
            delivery_fee=delivery_fee,
            discount=discount,
            payment_method=order_data.get('payment_method', 'Outros'),
            items_json=items_safe,
            sent_to_facebook=pixel_results['fb'],
            sent_to_google=pixel_results['ga'],
            driver_id=None,
            cash_opening_id=active_cash_id,
            delivery_type=delivery_type,
            notes=order_data.get('notes')
        )
        db.add(new_order)
        db.commit()
        
        # 5. CONFIRMAÇÃO NA ORIGEM (RESTAURADO E ATIVO)
        
        # A. iFood
        if "ifood" in str(order_data.get('payment_method', '')).lower():
            print(f"🤖 [Hub] Confirmando iFood #{ext_id}...")
            adapters = get_active_adapters(store)
            for adapter in adapters:
                if isinstance(adapter, IfoodAdapter):
                    try:
                        adapter.confirm_order(ext_id)
                    except Exception as e:
                        print(f"❌ [iFood] Erro ao confirmar: {e}")

        # B. Wabiz
        elif order_data.get('integration_source') == 'wabiz':
            print(f"🤖 [Hub] Confirmando Wabiz #{display_id}...")
            try:
                from services.integrations.wabiz import process_wabiz_update, WabizAdapter
                adapters = get_active_adapters(store)
                for adapter in adapters:
                    if isinstance(adapter, WabizAdapter):
                        process_wabiz_update(adapter, new_order, "CONFIRMADO")
            except Exception as e:
                print(f"⚠️ [Wabiz] Erro ao confirmar: {e}")

        # 6. Notificações
        try:
            trigger_url = f"http://127.0.0.1:8000/api/internal/kds-trigger/{store.id}"
            requests.post(trigger_url, timeout=5)
        except Exception as e:
            print(f"❌ [Hub] Erro KDS Trigger: {e}")
            
        
        # 7. RESGATE DE PIXEL PENDENTE (GA4)
        try:
            from models import PendingPixelEvent
            
            # CRIA LISTA DE IDs POSSÍVEIS (Para garantir o match)
            # O site pode ter enviado o ID Externo (UUID) ou o Display ID (Ex: 2829)
            possible_ids = [str(ext_id)]
            if display_id and str(display_id) != str(ext_id):
                possible_ids.append(str(display_id))

            print(f"🔍 [Hub] Procurando pixel pendente para IDs: {possible_ids}")

            pending_pixel = db.query(PendingPixelEvent).filter(
                PendingPixelEvent.store_id == store.id,
                PendingPixelEvent.event_id.in_(possible_ids), # <--- AQUI ESTÁ A CORREÇÃO (Usa .in_)
                PendingPixelEvent.status == 'PENDING'
            ).first()
            
            if pending_pixel:
                print(f"🔗 [Hub] MATCH! Pixel encontrado para o pedido #{pending_pixel.event_id}. Disparando GA4...")
                
                # ... (O resto do código de disparo continua igual ao anterior) ...
                web_data = pending_pixel.payload_json
                u_data = web_data.get('user_data', {})
                
                user_data_ga = {
                    "client_ip": u_data.get('ip'),
                    "client_user_agent": web_data.get('user_agent'),
                    "fbp": u_data.get('fbp'),
                    "fbc": u_data.get('fbc'),
                    "gclid": u_data.get('gclid'),
                    "session_id": u_data.get('session_id'),
                    "email": cust.get('email'),
                    "phone": cust.get('phone'),
                    "city": addr.get('city'),
                    "state": addr.get('state'),
                    "external_id": ext_id
                }

                dispatch_smart_event(
                    store, "Purchase", user_data_ga, items, total, ext_id, targets=["ga"]
                )
                
                pending_pixel.status = "PROCESSED"
                db.commit()
            else:
                print(f"💨 [Hub] Nenhum pixel pendente encontrado para {possible_ids}.")
                
        except Exception as e:
            print(f"⚠️ Erro ao processar pixel pendente: {e}")            
            

        if store.whatsapp_api_token and cust.get('phone'):
            try:
                from services.tasks import task_send_whatsapp
                first_name = cust['name'].split()[0].capitalize()
                task_send_whatsapp.delay(
                    phone=cust['phone'], template_name="pedido_fidelidade_v2",
                    variables=[first_name], store_token=store.whatsapp_api_token, phone_id=store.whatsapp_phone_id
                )
                new_order.sent_thank_you_msg = True
                db.commit()
            except: pass

    except Exception as e:
        print(f"🔥 [Hub] ERRO CRÍTICO PEDIDO: {str(e)}")
        import traceback
        traceback.print_exc()
            
            
# ==========================================
#        NOVO CRON DE SINCRONIZAÇÃO (MULTI)
# ==========================================
def sync_external_orders():
    """
    Busca pedidos em TODAS as lojas usando TODOS os adaptadores ativos.
    VERSÃO CORRIGIDA: Com Rollback para não travar a fila.
    """
    db = SessionLocal()
    try:
        stores = db.query(Store).filter(Store.is_open == True).all()
        
        for store in stores:
            adapters = get_active_adapters(store)
            if not adapters: continue

            for adapter in adapters:
                try:
                    raw_orders = adapter.fetch_orders()
                    if raw_orders:
                        for raw in raw_orders:
                            try:
                                # Normaliza
                                standard_order = adapter.normalize_order(raw)
                                # Processa (Transação Atômica)
                                process_standard_order(db, store, standard_order)
                            except Exception as e_proc:
                                db.rollback() # <--- CORREÇÃO: Limpa a transação se der erro neste pedido específico
                                print(f"❌ Erro ao salvar pedido {raw.get('id', '?')}: {e_proc}")
                                
                except Exception as e_adapter:
                    print(f"⚠️ Erro no adaptador: {e_adapter}")

    finally:
        db.close()
        
# --- CRONJOB DE OPORTUNIDADES ---
def run_opportunity_scanner():
    db = SessionLocal()
    try:
        stores = db.query(Store).filter(Store.is_open == True).all()
        for store in stores:
            brain = PizzaBrain(db, store.id)
            opportunity = brain.analyze_combos(min_support=3) 
            
            if opportunity:
                exists = db.query(Insight).filter(
                    Insight.store_id == store.id,
                    Insight.title == opportunity['title'],
                    Insight.is_archived == False
                ).first()
                
                if not exists:
                    insight = Insight(
                        store_id=store.id,
                        type="combo_opportunity",
                        title=opportunity['title'],
                        message=opportunity['message'],
                        action_prompt=opportunity['prompt']
                    )
                    db.add(insight)
                    db.commit()
    finally:
        db.close()
        
        
# --- RELATÓRIO MATINAL ---
def send_morning_reports():
    print("☕ [Cron] Preparando relatórios matinais...")
    db = SessionLocal()
    try:
        stores = db.query(Store).filter(
            Store.is_open == True,
            Store.whatsapp_api_token.isnot(None)
        ).all()
        
        for store in stores:
            target_phone = store.whatsapp_number 
            if not target_phone: continue

            brain = PizzaBrain(db, store.id)
            report_text = brain.generate_daily_report_text()
            
            if report_text:
                send_whatsapp_template(
                    phone_number=target_phone,
                    template_name="relatorio_diario_v1", 
                    variables=[report_text], 
                    store_token=store.whatsapp_api_token,
                    phone_id=store.whatsapp_phone_id
                )
    except Exception as e:
        print(f"❌ [Cron] Erro nos relatórios: {e}")
    finally:
        db.close()
        
        
# --- CRONJOB DE RFM ---
def run_rfm_analysis_cron():
    db = SessionLocal()
    try:
        stores = db.query(Store).filter(Store.is_open == True).all()
        for store in stores:
            task_run_rfm_analysis.delay(store.id)
    except Exception as e:
        print(f"❌ Erro ao agendar RFM: {e}")
    finally:
        db.close()