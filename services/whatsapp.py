import requests
import json

WA_API_VERSION = "v18.0"

def send_whatsapp_template(
    phone_number: str, 
    template_name: str, 
    store_token: str, 
    phone_id: str,
    variables: list = None,          # Variáveis do Corpo {{1}}, {{2}}
    location_data: dict = None       # Dados do Mapa (Opcional)
):
    """
    Envia mensagem via Template Oficial suportando Texto e Localização.
    """
    if not store_token or not phone_id:
        print("[WhatsApp] ❌ Sem credenciais.")
        return False

    url = f"https://graph.facebook.com/{WA_API_VERSION}/{phone_id}/messages"
    
    headers = {
        "Authorization": f"Bearer {store_token}",
        "Content-Type": "application/json"
    }

    clean_phone = "".join(filter(str.isdigit, str(phone_number)))
    if len(clean_phone) in [10, 11]: clean_phone = "55" + clean_phone

    # --- MONTAGEM DOS COMPONENTES ---
    components = []

    # 1. HEADER (Cabeçalho): Se tiver localização
    if location_data:
        components.append({
            "type": "header",
            "parameters": [{
                "type": "location",
                "location": {
                    "latitude": str(location_data.get('lat')),
                    "longitude": str(location_data.get('lng')),
                    "name": location_data.get('name'),
                    "address": location_data.get('address')
                }
            }]
        })

    # 2. BODY (Corpo): Texto com variáveis
    if variables:
        body_params = [{"type": "text", "text": str(var)} for var in variables]
        components.append({
            "type": "body",
            "parameters": body_params
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": clean_phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "pt_BR"},
            "components": components
        }
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        if response.status_code in [200, 201]:
            print(f"[WhatsApp] ✅ Template '{template_name}' enviado para {clean_phone}")
            return True
        else:
            print(f"[WhatsApp] ❌ Erro: {response.text}")
            return False
    except Exception as e:
        print(f"[WhatsApp] ❌ Exceção: {e}")
        return False
    
def notify_pickup_ready(store, customer_phone: str, customer_name: str, order_id: str):
    """
    Envia o template de 'Pedido Pronto'.
    Requisito: Você deve ter criado um template no painel do Facebook com o nome 'pedido_pronto'
    ou similar. Exemplo de texto do template:
    "Olá {{1}}, seu pedido #{{2}} já está pronto para retirada em nossa loja!"
    """
    if not store.whatsapp_api_token or not store.whatsapp_phone_id:
        return

    # Tenta enviar via Template (Recomendado para API Oficial)
    # Ajuste o nome "pedido_pronto" para o nome exato do seu template no Facebook
    success = send_whatsapp_template(
        phone_number=customer_phone,
        template_name="pedido_pronto", 
        store_token=store.whatsapp_api_token,
        phone_id=store.whatsapp_phone_id,
        variables=[customer_name, order_id] # Variáveis {{1}} e {{2}}
    )
    
    # Se você ainda não tem template aprovado e quer tentar enviar texto simples
    # (Só funciona se o cliente mandou msg nas últimas 24h)
    if not success:
        send_whatsapp_text(
            phone_number=customer_phone,
            message=f"Olá {customer_name}! Seu pedido #{order_id} está pronto para retirada! 🍕",
            store_token=store.whatsapp_api_token,
            phone_id=store.whatsapp_phone_id
        )

def send_whatsapp_text(phone_number: str, message: str, store_token: str, phone_id: str):
    """Envia mensagem de texto simples (Fallback)"""
    url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {store_token}",
        "Content-Type": "application/json"
    }
    clean_phone = "".join(filter(str.isdigit, str(phone_number)))
    if len(clean_phone) in [10, 11]: clean_phone = "55" + clean_phone
    
    payload = {
        "messaging_product": "whatsapp",
        "to": clean_phone,
        "type": "text",
        "text": {"body": message}
    }
    try:
        requests.post(url, headers=headers, json=payload, timeout=5)
    except: pass