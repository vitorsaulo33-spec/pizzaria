import requests
import os
from dotenv import load_dotenv

load_dotenv()

def send_to_google_analytics(
    event_name: str, 
    client_id: str, 
    user_data: dict, 
    custom_data: dict,
    measurement_id: str = None,
    api_secret: str = None,
    session_id: str = None,  # <--- NOVO
    gclid: str = None        # <--- NOVO
):
    """
    Envia eventos para GA4 via Measurement Protocol com suporte a Atribuição (GCLID/Session).
    """
    
    FINAL_MEASUREMENT_ID = measurement_id or os.getenv("GA4_MEASUREMENT_ID")
    FINAL_API_SECRET = api_secret or os.getenv("GA4_API_SECRET")

    if not FINAL_MEASUREMENT_ID or not FINAL_API_SECRET or "XXXX" in str(FINAL_MEASUREMENT_ID):
        print(f"[GA4] ⚠️ Credenciais inválidas. Ignorado.")
        return False

    url = f"https://www.google-analytics.com/mp/collect?measurement_id={FINAL_MEASUREMENT_ID}&api_secret={FINAL_API_SECRET}"

    ga4_event_map = {
        "Purchase": "purchase",
        "AddToCart": "add_to_cart",
        "ViewContent": "view_item",
        "InitiateCheckout": "begin_checkout"
    }
    
    ga4_name = ga4_event_map.get(event_name, event_name.lower().replace(" ", "_"))

    # Monta os parâmetros do evento
    event_params = {
        "currency": custom_data.get("currency", "BRL"),
        "value": custom_data.get("value", 0),
        "transaction_id": custom_data.get("transaction_id"),
        "items": custom_data.get("contents", []),
        "engagement_time_msec": "100" # Importante para aparecer como sessão engajada
    }

    # --- AQUI ESTÁ A CORREÇÃO DE ATRIBUIÇÃO ---
    if session_id:
        event_params["session_id"] = session_id
    
    if gclid:
        event_params["gclid"] = gclid
    # ------------------------------------------

    payload = {
        "client_id": client_id, 
        "events": [{
            "name": ga4_name,
            "params": event_params
        }]
    }

    try:
        r = requests.post(url, json=payload, timeout=5)
        if r.status_code in [200, 204]:
            print(f"[GA4] 📡 Enviado: {ga4_name} | GCLID: {bool(gclid)} | Session: {session_id}")
            return True 
        else:
            print(f"[GA4] ❌ Erro API ({r.status_code}): {r.text}")
            return False
            
    except Exception as e:
        print(f"[GA4] ❌ Exceção: {e}")
        return False