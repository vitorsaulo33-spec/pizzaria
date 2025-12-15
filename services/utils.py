import re
import hashlib
import pytz 
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import desc, or_, desc
from models import Event, Store, Customer, Address, CashOpening, CashClosing
from services.facebook import send_event_to_facebook, hash_data
from services.google import send_to_google_analytics

# --- FUNÇÕES DE PARSING E FORMATAÇÃO ---

def normalize_phone(raw_phone: str, raw_ddd: str = None, default_ddd: str = "13") -> str:
    """Padroniza telefone para 55 + DDD + Número"""
    phone = re.sub(r'\D', '', str(raw_phone or ""))
    ddd = re.sub(r'\D', '', str(raw_ddd or ""))
    
    if not phone: return ""

    if len(phone) <= 9:
        target_ddd = ddd if ddd else default_ddd
        return f"55{target_ddd}{phone}"
    elif len(phone) in [10, 11]:
        return f"55{phone}"
    return phone

def parse_wabiz_items(raw_items_list: list) -> list:
    """Limpa o JSON da Wabiz para uma lista plana"""
    items_clean = []
    for group in raw_items_list:
        for p in group.get('products', []):
            raw_name = p.get('name')
            parts = p.get('parts', [])
            
            full_title = "Item Diverso"
            if raw_name and str(raw_name).strip() != "None":
                full_title = raw_name.strip()
                if parts:
                    obs = " + ".join([part.get('name') for part in parts])
                    full_title = f"{full_title} ({obs})"
            elif parts:
                full_title = " + ".join([part.get('name') for part in parts])

            items_clean.append({
                "title": full_title,
                "quantity": float(p.get('qty', 1)),
                "price": float(p.get('price', 0))
            })
    return items_clean

def format_for_facebook(clean_items: list) -> list:
    """Formata para CAPI (Sem item_name)"""
    return [{
        "id": item.get('title'),
        "title": item.get('title'),
        "quantity": int(item.get('quantity', 1)),
        "item_price": item.get('price', 0)
    } for item in clean_items]

def format_for_google(clean_items: list) -> list:
    """Formata para GA4 (Com item_name)"""
    return [{
        "item_name": item.get('title'),
        "quantity": int(item.get('quantity', 1)),
        "price": item.get('price', 0)
    } for item in clean_items]

# --- FUNÇÕES DE BANCO DE DADOS (CRM) ---

def get_br_time():
    """Helper para pegar hora do Brasil"""
    return datetime.now(pytz.timezone('America/Sao_Paulo')).replace(tzinfo=None)

def upsert_customer_smart(db: Session, store_id: int, phone: str, name: str, email: str, value_to_add: float = 0):
    if not phone: return None
    
    # 1. SEGURANÇA: Cria lista de variações (com e sem 55) para evitar duplicidade
    clean = phone
    no_country = clean[2:] if clean.startswith("55") else clean
    possible_phones = list(set([clean, no_country]))

    # 2. Busca se JÁ EXISTE alguma dessas variações
    # CORREÇÃO: Busca todos os candidatos para evitar pegar o errado e causar colisão
    candidates = db.query(Customer).filter(
        Customer.store_id == store_id,
        Customer.phone.in_(possible_phones)
    ).all()
    
    customer = None
    
    # Prioridade 1: Tenta encontrar o que já tem o telefone EXATO
    for c in candidates:
        if c.phone == phone:
            customer = c
            break
            
    # Prioridade 2: Se não achou exato, pega qualquer um das variações (ex: sem 55)
    if not customer and candidates:
        customer = candidates[0]

    now_br = get_br_time() # Hora BR para corrigir o bug de data

    if not customer:
        # Cria Novo
        customer = Customer(
            store_id=store_id, phone=phone, name=name, email=email,
            total_spent=value_to_add, last_order_at=now_br
        )
        db.add(customer)
    else:
        # Atualiza Existente
        if customer.phone != phone: customer.phone = phone # Atualiza para o formato mais recente
        customer.name = name
        customer.email = email
        
        # Soma LTV com segurança
        current_total = customer.total_spent or 0.0
        customer.total_spent = current_total + value_to_add
        
        # Atualiza data
        customer.last_order_at = now_br
    
    db.commit()
    db.refresh(customer)
    return customer

def upsert_address(db: Session, customer_id: int, store_id: int, delivery_data: dict):
    street = delivery_data.get('address') or delivery_data.get('street')
    if not street: return None

    exists = db.query(Address).filter(
        Address.customer_id == customer_id,
        Address.street == street,
        Address.number == delivery_data.get('number')
    ).first()

    now_br = get_br_time()

    if not exists:
        new_addr = Address(
            customer_id=customer_id, store_id=store_id,
            street=street, number=delivery_data.get('number'),
            neighborhood=delivery_data.get('region') or delivery_data.get('neighborhood'),
            city=delivery_data.get('city'), state=delivery_data.get('state'),
            zip_code=delivery_data.get('postalCode') or delivery_data.get('zip_code'),
            complement=delivery_data.get('complement'), 
            last_used_at=now_br
        )
        db.add(new_addr)
    else:
        exists.last_used_at = now_br
    
    db.commit()

def recover_historical_ip(db: Session, store_id: int, phone: str, email: str):
    if not phone and not email: return None
    try:
        filters = []
        if phone: filters.append(Event.user_data['phone'].astext == phone)
        if email: filters.append(Event.user_data['email'].astext == email)
        
        if filters:
            last = db.query(Event).filter(
                Event.store_id == store_id, Event.client_ip.isnot(None), or_(*filters)
            ).order_by(desc(Event.created_at)).first()
            return last.client_ip if last else None
    except: return None

# O despachante universal continua igual...
def dispatch_smart_event(
    store: Store, 
    event_name: str, 
    user_data: dict, 
    items: list, 
    total_value: float, 
    transaction_id: str,
    targets: list = ["fb", "ga"] # <--- Padrão: Manda para ambos
):
    results = {"fb": False, "ga": False}
    
    # 1. Facebook CAPI (Só executa se 'fb' estiver na lista targets)
    if "fb" in targets and store.fb_pixel_id and store.fb_access_token:
        try:
            fb_contents = format_for_facebook(items)
            
            # Prepara dados do usuário Facebook
            fb_user = {
                "email": user_data.get('email'),
                "phone": user_data.get('phone'),
                "first_name": user_data.get('first_name'),
                "last_name": user_data.get('last_name'),
                "city": user_data.get('city'),
                "zip_code": user_data.get('zip_code'),
                "state": user_data.get('state'),
                "country": "br",
                "client_ip": user_data.get('ip'), # Mapeia IP corretamente
                "client_user_agent": user_data.get('user_agent'), # Mapeia User Agent corretamente
                "fbp": user_data.get('fbp'),
                "fbc": user_data.get('fbc'),
                "external_id": user_data.get('external_id') or user_data.get('email')
            }

            fb_custom = {
                "currency": "BRL",
                "value": total_value,
                "content_ids": [item.get('id', '') for item in fb_contents],
                "content_type": "product",
                "contents": fb_contents,
                "num_items": sum(item.get('quantity', 1) for item in fb_contents),
                "order_id": transaction_id
            }

            results["fb"] = send_event_to_facebook(
                pixel_id=store.fb_pixel_id,
                access_token=store.fb_access_token,
                event_name=event_name,
                event_id=transaction_id, # Deduplicação
                event_source_url=user_data.get('url'),
                user_data=fb_user,
                custom_data=fb_custom
            )
        except Exception as e:
            print(f"⚠️ [Dispatcher] Erro FB: {e}")

    # 2. Google GA4 (Só executa se 'ga' estiver na lista targets)
    if "ga" in targets and store.ga4_measurement_id and store.ga4_api_secret:
        try:
            ga_items = format_for_google(items)
            
            # Gera client_id consistente
            raw_id = f"{transaction_id}{user_data.get('phone')}"
            ga_client_id = hashlib.md5(raw_id.encode()).hexdigest()
            
            ga_user = {
                "em": hash_data(user_data.get('email')),
                "ph": hash_data(user_data.get('phone')),
                "ct": hash_data(user_data.get('city')),
                "country": hash_data("BR")
            }
            
            ga_custom = {
                "currency": "BRL",
                "value": total_value,
                "transaction_id": transaction_id,
                "contents": ga_items
            }
            
            # Extração de Atribuição (Vindo do GTM)
            session_id = user_data.get('session_id')
            gclid = user_data.get('gclid')

            results["ga"] = send_to_google_analytics(
                event_name, ga_client_id, ga_user, ga_custom,
                measurement_id=store.ga4_measurement_id, 
                api_secret=store.ga4_api_secret,
                session_id=session_id, # <--- Importante para Google Ads
                gclid=gclid            # <--- Importante para Google Ads
            )
        except Exception as e:
            print(f"⚠️ [Dispatcher] Erro GA4: {e}")

    return results


def get_active_cash_id(db, store_id):
    """
    Retorna o ID do caixa ABERTO para a loja.
    Retorna None se estiver fechado ou não existir.
    """
    # 1. Pega a última abertura
    last_opening = db.query(CashOpening).filter(
        CashOpening.store_id == store_id
    ).order_by(desc(CashOpening.created_at)).first()
    
    if not last_opening:
        return None
        
    # 2. Verifica se houve um fechamento posterior
    last_closing = db.query(CashClosing).filter(
        CashClosing.store_id == store_id,
        CashClosing.closed_at > last_opening.created_at
    ).first()
    
    # Se tem fechamento depois da abertura, então está FECHADO.
    if last_closing:
        return None
    
    return last_opening.id