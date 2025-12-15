import hashlib
import requests
import os
import time
from typing import Dict, Any
import re
from dotenv import load_dotenv

# Garante o carregamento do .env localmente (Útil para o FB_TEST_CODE global)
load_dotenv()

FB_API_VERSION = "v21.0"
FB_TEST_CODE = os.getenv("FB_TEST_CODE", "")

def hash_data(data: str) -> str:
    """
    Normaliza e gera o Hash SHA256 do dado.
    Facebook exige: minúsculo, sem espaços, SHA256.
    """
    if not data:
        return None
    clean_data = str(data).strip().lower()
    # Se já parece um hash (64 chars hex), retorna ele mesmo
    if len(clean_data) == 64 and all(c in '0123456789abcdef' for c in clean_data):
        return clean_data
    return hashlib.sha256(clean_data.encode('utf-8')).hexdigest()

def send_event_to_facebook(
    event_name: str, 
    user_data: Dict, 
    custom_data: Dict = None, 
    event_id: str = None, 
    pixel_id: str = None,      # <--- SaaS: Recebe Pixel da Loja
    access_token: str = None,  # <--- SaaS: Recebe Token da Loja
    event_source_url: str = None # <--- NOVO ARGUMENTO OBRIGATÓRIO PARA CORRIGIR O ERRO
):
    """
    Envia evento para API de Conversões (CAPI) suportando Multi-Tenant (SaaS).
    Retorna: True (Sucesso) ou False (Falha).
    """
    
    # --- TRAVA DE SEGURANÇA ---
    if not pixel_id or not access_token:
        print(f"[Facebook] ❌ ERRO CRÍTICO: Pixel ID ou Token não fornecidos para o evento {event_name}!")
        return False
    # --------------------------

    url = f"https://graph.facebook.com/{FB_API_VERSION}/{pixel_id}/events"
    
    # --- 1. LÓGICA DE TRATAMENTO DE DADOS (CRUCIAL) ---
    
    # Tratamento de External ID (Prioridade: ID enviado > Hash do Email)
    ext_id = user_data.get("external_id")
    if not ext_id and user_data.get("email"):
        ext_id = hash_data(user_data.get("email"))
        
    # Prepara o telefone (Remove não dígitos e adiciona 55 se necessário)
    raw_phone = user_data.get("phone")
    if raw_phone:
        # Remove tudo que não for número
        raw_phone = re.sub(r'\D', '', str(raw_phone))
        # Se não for um hash e tiver tamanho de cel BR (10 ou 11), adiciona DDI 55
        if len(raw_phone) in [10, 11]: 
            raw_phone = '55' + raw_phone

    # Monta o User Data Payload com Hashing
    user_payload = {
        "em": hash_data(user_data.get("email")),
        "ph": hash_data(raw_phone),
        "fn": hash_data(user_data.get("first_name")),
        "ln": hash_data(user_data.get("last_name")),
        "ct": hash_data(user_data.get("city")),
        "st": hash_data(user_data.get("state")),
        "zp": hash_data(user_data.get("zip_code")),
        "country": hash_data(user_data.get("country")),
        "client_ip_address": user_data.get("client_ip"),
        "client_user_agent": user_data.get("client_user_agent"),
        "fbp": user_data.get("fbp"),
        "fbc": user_data.get("fbc"),
        "external_id": ext_id
    }
    
    # Remove chaves vazias para não enviar lixo
    user_payload = {k: v for k, v in user_payload.items() if v is not None}

    # --- 2. MONTAGEM DO PAYLOAD FINAL ---
    payload = {
        "data": [
            {
                "event_name": event_name,
                "event_time": int(time.time()),
                "event_id": event_id,
                "action_source": "website",
                "user_data": user_payload,
                "custom_data": custom_data or {},
                # AQUI ESTÁ A CORREÇÃO: Usa o argumento que recebemos na função
                "event_source_url": event_source_url 
            }
        ],
        "access_token": access_token 
    }

    # Mantém suporte a código de teste (Global ou passado via custom_data se quisesse evoluir)
    if FB_TEST_CODE:
        payload["test_event_code"] = FB_TEST_CODE

    # --- 3. ENVIO ---
    try:
        r = requests.post(url, json=payload, timeout=5)
        
        if r.status_code == 200:
            mode = f"(MODO TESTE: {FB_TEST_CODE})" if FB_TEST_CODE else ""
            print(f"[Facebook] ✅ Sucesso: {event_name} | Pixel: {pixel_id} {mode}")
            return True
        else:
            print(f"[Facebook] ❌ Erro API ({r.status_code}): {r.text}")
            return False
            
    except Exception as e:
        print(f"[Facebook] ❌ Exceção de Conexão: {e}")
        return False