from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta
from collections import Counter
from models import Order, Campaign, CampaignLog, Store, Customer, Address
from services.whatsapp import send_whatsapp_template

def get_favorite_product(db: Session, store_id: int, phone: str):
    orders = db.query(Order).filter(
        Order.store_id == store_id,
        Order.customer_phone == phone
    ).all()

    if not orders: return "Pizza Especial"

    IGNORE_TERMS = ["coca", "guaraná", "fanta", "sprite", "h2oh", "agua", "água", "cerveja", "suco", "refrigerante", "entrega", "taxa", "borda"]
    all_items = []
    
    for order in orders:
        if not order.items_json: continue
        for item in order.items_json:
            name = item.get('title', '').strip()
            if not name or name == "None": continue
            clean_name = name.split('(')[0].strip()
            if any(term in clean_name.lower() for term in IGNORE_TERMS): continue
            all_items.append(clean_name)

    if not all_items: return "Pizza"
    return Counter(all_items).most_common(1)[0][0]

#FUNÇÃO PARA MANDAR MENSAGENS PARA ANIVERSARIANTES

def process_birthday_customers(db: Session, campaign: Campaign, store: Store):
    """
    Envia parabéns para aniversariantes do dia.
    """
    today = datetime.now()
    
    # Busca clientes que fazem aniversário hoje (mesmo dia e mês)
    # Nota: Dependendo do banco (Postgres/SQLite), a função extract muda.
    # Aqui assumindo Postgres:
    birthday_people = db.query(Customer).filter(
        Customer.store_id == store.id,
        func.extract('month', Customer.birth_date) == today.month,
        func.extract('day', Customer.birth_date) == today.day
    ).all()
    
    print(f"   🎂 Aniversários '{campaign.name}': {len(birthday_people)} hoje.")

    for client in birthday_people:
        # Verifica se já mandou hoje (para não repetir se rodar o script 2x)
        last_log = db.query(CampaignLog).filter(
            CampaignLog.campaign_id == campaign.id,
            CampaignLog.customer_phone == client.phone,
            func.date(CampaignLog.sent_at) == today.date()
        ).first()
        
        if last_log: continue

        if store.whatsapp_api_token:
            first_name = client.name.split()[0].capitalize()
            # Template: parabens_cliente_v1
            # Texto: "Parabéns {{1}}! 🎂 Hoje é seu dia..."
            sent = send_whatsapp_template(
                phone_number=client.phone,
                template_name="parabens_cliente_v1",
                variables=[first_name],
                store_token=store.whatsapp_api_token,
                phone_id=store.whatsapp_phone_id
            )
            
            if sent:
                log = CampaignLog(
                    campaign_id=campaign.id,
                    customer_phone=client.phone,
                    customer_name=client.name,
                    status="sent",
                    message_id="BIRTHDAY"
                )
                db.add(log)
                db.commit()
                
def process_nps_automations(db: Session, campaign: Campaign, store: Store):
    """
    Envia pesquisa X horas após o pedido.
    """
    # Janela de tempo: Pedidos feitos entre (Agora - Delay - 1h) e (Agora - Delay)
    # Ex: Se delay=2h, pega pedidos feitos entre 3h e 2h atrás.
    delay_hours = campaign.days_delay # Usaremos este campo como HORAS para este tipo
    if delay_hours == 0: delay_hours = 2 # Padrão 2h
    
    time_threshold = datetime.now() - timedelta(hours=delay_hours)
    time_start = time_threshold - timedelta(minutes=59) # Janela de 1h

    orders = db.query(Order).filter(
        Order.store_id == store.id,
        Order.sent_nps == False, # Ainda não enviou
        Order.created_at >= time_start,
        Order.created_at <= time_threshold
    ).all()

    print(f"   ⭐ NPS '{campaign.name}': {len(orders)} pedidos para avaliar.")

    for order in orders:
        if not order.customer_phone: continue
        
        # Envia
        if store.whatsapp_api_token and store.whatsapp_phone_id:
            first_name = order.customer_name.split()[0].capitalize() if order.customer_name else "Cliente"
            
            # Template NPS deve ter botões: "1 ⭐", "3 ⭐", "5 ⭐"
            # Nome sugerido na Meta: pesquisa_nps_v1
            template_name = "pesquisa_nps_v1" 

            sent = send_whatsapp_template(
                phone_number=order.customer_phone,
                template_name=template_name,
                variables=[first_name],
                store_token=store.whatsapp_api_token,
                phone_id=store.whatsapp_phone_id
            )
            
            if sent:
                order.sent_nps = True
                db.commit()
                
                # Log
                log = CampaignLog(
                    campaign_id=campaign.id,
                    customer_phone=order.customer_phone,
                    customer_name=order.customer_name,
                    status="sent",
                    message_id="NPS"
                )
                db.add(log)
                db.commit()
                


def run_crm_automations(db: Session):
    """
    Gerenciador Central de Automações.
    Roda a cada hora via Scheduler.
    """
    current_hour = datetime.now().hour
    print(f"🤖 [CRM] Iniciando ciclo das {current_hour}:00h...")
    
    # Busca todas as campanhas ativas
    campaigns = db.query(Campaign).join(Store).filter(Campaign.is_active == True).all()
    
    if not campaigns:
        print("   💤 Nenhuma campanha ativa no sistema.")
        return

    for campaign in campaigns:
        store = campaign.store
        if not store.whatsapp_api_token: continue # Pula se loja não tem whats configurado

        # === GATILHO 1: PÓS-VENDA (NPS) ===
        # Roda toda hora, pois depende do tempo exato da entrega
        if campaign.trigger_type == "post_sale":
            process_nps_automations(db, campaign, store)

        # === GATILHO 2: BROADCAST AGENDADO ===
        # Roda se a data/hora chegou ou passou
        elif campaign.trigger_type == "broadcast" and campaign.scheduled_at:
            if datetime.now() >= campaign.scheduled_at:
                process_broadcast_campaign(db, campaign, store)

        # === GATILHO 3: RECORRENTES (Inativos / Aniversário) ===
        # Só roda se for a "Hora Mágica" configurada pela loja (ex: 18h)
        elif store.crm_schedule_hour == current_hour:
            
            if campaign.trigger_type == "inactive":
                process_inactive_customers(db, campaign, store)
                
            elif campaign.trigger_type == "birthday":
                process_birthday_customers(db, campaign, store)


def process_broadcast_campaign(db: Session, campaign: Campaign, store: Store):
    """
    Processa disparo em massa com filtros avançados (Bairro, Valor, Data).
    """
    print(f"   📢 Iniciando Broadcast: {campaign.name}")
    
    # 1. Query Base: Clientes da loja com telefone
    query = db.query(Customer).filter(
        Customer.store_id == store.id,
        Customer.phone.isnot(None)
    )
    
    # 2. Aplica Filtros (Segmentação)
    rules = campaign.filter_rules or {}
    
    # A. Filtro Financeiro (VIPs)
    min_spent = rules.get('min_spent')
    if min_spent and float(min_spent) > 0:
        query = query.filter(Customer.total_spent >= float(min_spent))
        print(f"      -> Segmento: Gasto > R$ {min_spent}")

    # B. Filtro de Recência (Ativos/Inativos)
    last_days = rules.get('last_order_days')
    if last_days and int(last_days) > 0:
        # Ex: Comprou nos últimos 30 dias
        limit_date = datetime.now() - timedelta(days=int(last_days))
        query = query.filter(Customer.last_order_at >= limit_date)
        print(f"      -> Segmento: Comprou nos últimos {last_days} dias")

    # C. Filtro de Bairro (Geográfico)
    target_neighborhoods = rules.get('neighborhoods')
    if target_neighborhoods and len(target_neighborhoods) > 0:
        # Join com Address para filtrar por bairro
        query = query.join(Address).filter(
            Address.neighborhood.in_(target_neighborhoods)
        ).distinct()
        print(f"      -> Segmento: {len(target_neighborhoods)} bairros selecionados")

    targets = query.all()
    
    if not targets:
        print("      ⚠️ Nenhum cliente encontrado com esses filtros. Encerrando campanha.")
        campaign.is_active = False 
        db.commit()
        return

    print(f"      🎯 Público Alvo: {len(targets)} clientes.")

    # 3. Disparo
    count_sent = 0
    template_name = campaign.meta_template_name or "promocao_padrao_v1"

    for client in targets:
        # Trava de Segurança: Já recebeu esse broadcast?
        # Importante para não mandar repetido se o script rodar de novo
        already_sent = db.query(CampaignLog).filter(
            CampaignLog.campaign_id == campaign.id,
            CampaignLog.customer_phone == client.phone
        ).first()
        
        if already_sent: continue

        first_name = client.name.split()[0].capitalize() if client.name else "Cliente"
        
        # Em Broadcast, geralmente usamos apenas o Nome como variável {{1}}
        # Se seu template tiver mais variáveis, precisa ajustar aqui
        variables = [first_name]

        try:
            sent = send_whatsapp_template(
                client.phone, template_name, variables, 
                store.whatsapp_api_token, store.whatsapp_phone_id
            )
            
            if sent:
                log = CampaignLog(
                    campaign_id=campaign.id,
                    customer_phone=client.phone,
                    customer_name=client.name,
                    status="sent",
                    message_id="BROADCAST"
                )
                db.add(log)
                db.commit() # Commit a cada envio para salvar progresso
                count_sent += 1
                
        except Exception as e:
            print(f"      ❌ Erro ao enviar para {client.phone}: {e}")

    print(f"   ✅ Broadcast finalizado. {count_sent} mensagens enviadas.")
    
    # 4. Desativa a campanha para não rodar no próximo ciclo
    campaign.is_active = False
    db.commit()
            
            

def process_inactive_customers(db: Session, campaign: Campaign, store: Store):
    limit_date = datetime.now() - timedelta(days=campaign.days_delay)
    
    subquery = db.query(
        Order.customer_phone,
        func.max(Order.created_at).label('last_purchase'),
        func.max(Order.customer_name).label('customer_name')
    ).filter(Order.store_id == store.id).group_by(Order.customer_phone).subquery()

    inactive_candidates = db.query(subquery).filter(subquery.c.last_purchase <= limit_date).all()
    
    print(f"   ➤ Campanha '{campaign.name}': {len(inactive_candidates)} candidatos.")

    for row in inactive_candidates:
        phone = row.customer_phone
        name = row.customer_name
        if not phone: continue

        # Janela de silêncio (30 dias)
        last_log = db.query(CampaignLog).filter(
            CampaignLog.campaign_id == campaign.id,
            CampaignLog.customer_phone == phone,
            CampaignLog.sent_at >= (datetime.now() - timedelta(days=30)) 
        ).first()

        if last_log: continue

        # Personalização
        fav_product = get_favorite_product(db, store.id, phone)
        first_name = name.split()[0].capitalize() if name else "Cliente"
        
        # --- ENVIO OFICIAL VIA TEMPLATE ---
        if store.whatsapp_api_token and store.whatsapp_phone_id:
            # AQUI ESTÁ A CORREÇÃO:
            # Usamos o nome do template que definimos (ou que está no banco se você atualizar lá)
            # Por segurança, vamos forçar o nome 'recuperacao_inativos' se o do banco for texto livre
            template_name = campaign.meta_template_name 
            
            # Fallback de segurança se estiver vazio
            if not template_name:
                print(f"⚠️ Campanha {campaign.name} sem nome de template Meta configurado.")
                continue

            print(f"💬 [CRM] Enviando template '{template_name}' para {first_name}...")
            
            sent = send_whatsapp_template(
                phone_number=phone,
                template_name=template_name,
                variables=[first_name, fav_product], # Assume que todo template de inativo usa 2 vars
                store_token=store.whatsapp_api_token,
                phone_id=store.whatsapp_phone_id
            )
            
            if sent:
                log = CampaignLog(
                    campaign_id=campaign.id,
                    customer_phone=phone,
                    customer_name=name,
                    status="sent",
                    message_id="META_API"
                )
                db.add(log)
                db.commit()