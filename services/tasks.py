# Arquivo: pizzaria/services/tasks.py
from celery_app import celery_app
from database import SessionLocal
from services.whatsapp import send_whatsapp_template
from models import Store, Customer, Campaign, CampaignLog, Address
from services.analytics import PizzaBrain
from datetime import datetime

# Helper para abrir e fechar banco dentro da task
def get_db_session():
    return SessionLocal()

@celery_app.task(name="send_whatsapp_async")
def task_send_whatsapp(phone: str, template_name: str, variables: list, store_token: str, phone_id: str):
    """
    Envia UMA mensagem de WhatsApp de forma assíncrona.
    """
    print(f"📨 [Celery] Enviando Whats para {phone}...")
    try:
        send_whatsapp_template(
            phone_number=phone,
            template_name=template_name,
            variables=variables,
            store_token=store_token,
            phone_id=phone_id
        )
        return "Enviado"
    except Exception as e:
        print(f"❌ [Celery] Erro no envio: {e}")
        return "Erro"

@celery_app.task(name="run_rfm_async")
def task_run_rfm_analysis(store_id: int):
    """
    Roda a análise pesada de RFM para uma loja específica.
    """
    db = get_db_session()
    try:
        print(f"🧠 [Celery] Iniciando RFM para Loja ID {store_id}...")
        brain = PizzaBrain(db, store_id)
        processed = brain.run_rfm_segmentation()
        print(f"✅ [Celery] RFM Concluído. {processed} clientes analisados.")
        return processed
    finally:
        db.close()

@celery_app.task(name="broadcast_campaign_async")
def task_process_broadcast(campaign_id: int):
    """
    Dispara uma campanha em massa respeitando os filtros de segmento.
    """
    db = get_db_session()
    try:
        campaign = db.query(Campaign).get(campaign_id)
        if not campaign or not campaign.store.whatsapp_api_token:
            print(f"❌ [Celery] Campanha {campaign_id} inválida ou loja sem token.")
            return "Falha"

        store = campaign.store
        
        # --- 1. APLICAÇÃO DOS FILTROS (Igual ao crm_engine) ---
        query = db.query(Customer).filter(
            Customer.store_id == store.id,
            Customer.phone.isnot(None)
        )
        
        rules = campaign.filter_rules or {}
        
        # A. Filtro Financeiro
        if rules.get('min_spent'):
            query = query.filter(Customer.total_spent >= float(rules['min_spent']))

        # B. Filtro de Recência
        if rules.get('last_order_days'):
            from datetime import timedelta
            limit_date = datetime.now() - timedelta(days=int(rules['last_order_days']))
            query = query.filter(Customer.last_order_at >= limit_date)

        # C. Filtro de Bairro
        if rules.get('neighborhoods') and len(rules['neighborhoods']) > 0:
            query = query.join(Address).filter(
                Address.neighborhood.in_(rules['neighborhoods'])
            ).distinct()

        targets = query.all()
        
        print(f"📢 [Celery] Disparando Campanha '{campaign.name}' para {len(targets)} alvos (Filtrados).")
        
        count = 0
        for client in targets:
            # Trava de Segurança: Verifica se já enviou para este cliente nesta campanha
            # Isso evita duplicidade se a task reiniciar
            already_sent = db.query(CampaignLog).filter(
                CampaignLog.campaign_id == campaign.id,
                CampaignLog.customer_phone == client.phone
            ).first()
            
            if already_sent: continue

            first_name = client.name.split()[0].capitalize() if client.name else "Cliente"
            
            success = send_whatsapp_template(
                client.phone, 
                campaign.meta_template_name, 
                [first_name], # Variáveis (ajuste se seu template usar mais)
                store.whatsapp_api_token, 
                store.whatsapp_phone_id
            )
            
            if success:
                log = CampaignLog(
                    campaign_id=campaign.id,
                    customer_phone=client.phone,
                    customer_name=client.name,
                    status="sent",
                    message_id="CELERY_BROADCAST"
                )
                db.add(log)
                
                # Commit a cada envio (ou a cada 10) para salvar o progresso
                # Isso impede reenvio em caso de crash
                db.commit() 
                count += 1
                
        return f"Finalizado: {count} envios."
        
    except Exception as e:
        print(f"❌ [Celery] Erro crítico no broadcast: {e}")
        db.rollback()
        return "Erro Crítico"
    finally:
        db.close()