# Arquivo: pizzaria/services/integrations/ifood.py
from fastapi import status
import requests
import pytz
import re
from datetime import datetime
from typing import List, Dict, Any
from .base import BaseIntegration
from models import Order
import traceback

IFOOD_AUTH_URL = "https://merchant-api.ifood.com.br/authentication/v1.0/oauth/token"
IFOOD_BASE_URL = "https://merchant-api.ifood.com.br/order/v1.0"

class IfoodAdapter(BaseIntegration):
    
    def __init__(self, user: str, password: str, merchant_id: str):
        self.client_id = user
        self.client_secret = password
        self.merchant_id = merchant_id
        self.token = None

    def _authenticate(self):
        try:
            payload = {
                "grantType": "client_credentials",
                "clientId": self.client_id,
                "clientSecret": self.client_secret
            }
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            
            resp = requests.post(IFOOD_AUTH_URL, data=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                self.token = resp.json().get("accessToken")
                return True
            print(f"❌ [iFood] Erro Auth: {resp.text}")
            return False
        except Exception as e:
            # --- ALTERAÇÃO: Log completo do erro ---
            print(f"❌ [iFood] Erro Conexão Auth: {e}")
            traceback.print_exc() # Imprime a linha exata do erro no terminal
            # ---------------------------------------
            return False
        
    def _get_headers(self):
        if not self.token:
            if not self._authenticate(): return None
        return {"Authorization": f"Bearer {self.token}"}
    
    # --- AÇÕES DE PEDIDO (COM LOGS DETALHADOS) ---
    def confirm_order(self, order_id: str):
        headers = self._get_headers()
        if not headers: return False
        try:
            r = requests.post(f"{IFOOD_BASE_URL}/orders/{order_id}/confirm", headers=headers, timeout=5)
            if r.status_code == 202: return True
            print(f"❌ [iFood] Falha Confirm ({r.status_code}): {r.text}")
            return False
        except Exception as e:
            print(f"❌ [iFood] Erro Conexão Confirm: {e}")
            return False

    def dispatch_order(self, order_id: str):
        headers = self._get_headers()
        if not headers: return False
        try:
            r = requests.post(f"{IFOOD_BASE_URL}/orders/{order_id}/dispatch", headers=headers, timeout=5)
            if r.status_code == 202:
                print(f"✅ [iFood] Pedido {order_id} despachado com sucesso!")
                return True
            print(f"❌ [iFood] Falha Dispatch ({r.status_code}): {r.text}")
            return False
        except Exception as e:
            print(f"❌ [iFood] Erro Conexão Dispatch: {e}")
            return False

    def ready_to_pickup(self, order_id: str):
        headers = self._get_headers()
        if not headers: return False
        try: 
            r = requests.post(f"{IFOOD_BASE_URL}/orders/{order_id}/readyToPickup", headers=headers, timeout=5)
            if r.status_code == 202: return True
            print(f"❌ [iFood] Falha ReadyToPickup ({r.status_code}): {r.text}")
            return False
        except Exception as e: 
            print(f"❌ [iFood] Erro Conexão ReadyToPickup: {e}")
            return False

    def request_cancellation(self, order_id: str, reason_code: str, details: str):
        headers = self._get_headers()
        if not headers: return False
        try:
            payload = {"reason": reason_code, "cancellationCode": reason_code, "details": details}
            return requests.post(f"{IFOOD_BASE_URL}/orders/{order_id}/requestCancellation", json=payload, headers=headers, timeout=5).status_code == 202
        except: return False

    # --- POLLING DE EVENTOS ---
    def fetch_orders(self) -> List[Dict[str, Any]]:
        headers = self._get_headers()
        if not headers: return []

        orders_found = []
        acks = []

        try:
            url_events = f"{IFOOD_BASE_URL}/events:polling"
            resp = requests.get(url_events, headers=headers, timeout=10)
            
            if resp.status_code == 200:
                events = resp.json() or []
                if not events: return []

                for ev in events:
                    order_id = ev.get('orderId')
                    code = ev.get('code')
                    acks.append({"id": ev.get('id')}) 

                    if not order_id: continue

                    if code == 'PLC':
                        r_det = requests.get(f"{IFOOD_BASE_URL}/orders/{order_id}", headers=headers, timeout=10)
                        if r_det.status_code == 200:
                            full_order = r_det.json()
                            full_order['event_code'] = 'PLC'
                            orders_found.append(full_order)
                    
                    elif code == 'CAN':
                        orders_found.append({
                            'id': order_id,
                            'event_code': 'CAN',
                            'reason': ev.get('metadata', {}).get('reason', 'Cancelado pelo iFood')
                        })

                if acks:
                    requests.post(f"{IFOOD_BASE_URL}/events/acknowledgment", json=acks, headers=headers)
            
            elif resp.status_code == 401:
                self.token = None 
                
            return orders_found

        except Exception as e:
            print(f"❌ [iFood] Erro Fetch: {e}")
            return []

    def normalize_order(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Traduz iFood JSON (3 Níveis) -> ALIV Standard (Dados Puros/Hierárquicos)"""
        
        # 1. Tratamento de Cancelamento
        if raw.get('event_code') == 'CAN':
            return {
                "external_id": str(raw.get('id')),
                "status": "CANCELADO",
                "notes": raw.get('reason'),
                "is_update": True
            }
        
        def get_safe(obj, key):
            if not isinstance(obj, dict): return {}
            val = obj.get(key, {})
            return val if isinstance(val, dict) else {}

        # 2. Dados Financeiros e Cliente
        
        created_at_utc = datetime.utcnow()
        
        cust = get_safe(raw, 'customer')
        phone_obj = get_safe(cust, 'phone')
        dlv = get_safe(raw, 'delivery')
        dlv_addr = get_safe(dlv, 'deliveryAddress')
        total_obj = get_safe(raw, 'total')
        
        # Valores Monetários
        try: order_total = float(total_obj.get('orderAmount', 0))
        except: order_total = 0.0
        try: delivery_fee = float(total_obj.get('deliveryFee', 0))
        except: delivery_fee = 0.0
        try: discount = float(total_obj.get('benefits', 0))
        except: discount = 0.0
        
        # Pagamento
        payments_obj = get_safe(raw, 'payments')
        methods = payments_obj.get('methods', [])
        payment_method_str = "iFood Online"
        
        if methods:
            main_method = methods[0]
            type_pay = str(main_method.get('type', '')).upper()
            if type_pay == 'OFFLINE':
                desc = get_safe(main_method, 'method').get('description', 'Maquininha')
                payment_method_str = f"{desc} (iFood)"
                if "PIX" in str(desc).upper(): payment_method_str = "Pix (iFood)"

        # --- 3. PROCESSAMENTO DE ITENS (NÍVEL 1, 2 e 3) ---
        clean_items = []
        raw_items = raw.get('items', [])
        
        if isinstance(raw_items, list):
            for item in raw_items: # NÍVEL 1: O Produto (Ex: Pizza, Combo)
                if not isinstance(item, dict): continue
                
                raw_title = item.get('name', 'Item')
                raw_title = re.sub(r'^\d+\s*-\s*', '', raw_title) 

                structured_parts = []
                structured_addons = []
                
                options = item.get('options', [])
                if isinstance(options, list):
                    for sub in options: # NÍVEL 2: A Opção
                        if not isinstance(sub, dict): continue
                        
                        sub_name = sub.get('name', '')
                        sub_price = float(sub.get('price', 0))
                        sub_qty = float(sub.get('quantity', 1))
                        # CAPTURA DE CÓDIGO NÍVEL 2 (CRUCIAL PARA O BANCO)
                        sub_code = str(sub.get('externalCode') or sub.get('code') or sub.get('ean') or '')
                        
                        # --- TRATAMENTO NÍVEL 3 (CUSTOMIZATIONS) ---
                        sub_items_data = [] 
                        l3_price_sum = 0.0
                        
                        customizations = sub.get('customizations', [])
                        
                        if isinstance(customizations, list):
                            for cust in customizations: # NÍVEL 3: Detalhe
                                c_name = cust.get('name', '')
                                c_price = float(cust.get('price', 0))
                                c_qty = float(cust.get('quantity', 1))
                                # CAPTURA DE CÓDIGO NÍVEL 3 (CRUCIAL PARA ADICIONAIS)
                                c_code = str(cust.get('externalCode') or cust.get('code') or cust.get('ean') or '')
                                
                                sub_items_data.append({
                                    "name": c_name,
                                    "quantity": c_qty,
                                    "price": c_price,
                                    "external_code": c_code  # <--- AQUI
                                })
                                
                                l3_price_sum += (c_price * c_qty)

                        sub_total_price = sub_price + l3_price_sum

                        # Classificação
                        is_flavor = "pizza" in raw_title.lower() and "borda" not in sub_name.lower() and "refrigerante" not in sub_name.lower()
                        
                        item_data = {
                            "name": sub_name,
                            "quantity": sub_qty,
                            "price": sub_total_price,
                            "external_code": sub_code, # <--- AQUI
                            "sub_items": sub_items_data 
                        }

                        if is_flavor:
                            structured_parts.append(item_data)
                        else:
                            structured_addons.append(item_data)

                try: i_qty = float(item.get('quantity', 1))
                except: i_qty = 1.0
                try: i_price = float(item.get('unitPrice', 0)) 
                except: i_price = 0.0

                clean_items.append({
                    "title": raw_title,
                    "quantity": i_qty,
                    "price": i_price, 
                    "external_code": str(item.get('externalCode') or item.get('id', '')),
                    "parts": structured_parts,   
                    "addons": structured_addons, 
                    "notes": item.get('observations', '')
                })

        return {
            "external_id": str(raw.get('id')),
            "display_id": str(raw.get('displayId', raw.get('id'))),
            "total_value": order_total,
            "delivery_fee": delivery_fee,
            "discount": discount,
            "created_at": created_at_utc,
            "customer": {
                "name": cust.get('name', 'Cliente iFood'),
                "phone": phone_obj.get('number', ''), 
                "email": "" 
            },
            "address": {
                "street": dlv_addr.get('streetName', ''),
                "number": dlv_addr.get('streetNumber', ''),
                "neighborhood": dlv_addr.get('neighborhood', ''),
                "city": dlv_addr.get('city', ''),
                "state": dlv_addr.get('state', ''),
                "zip_code": dlv_addr.get('postalCode', '')
            },
            "payment_method": payment_method_str,
            "items": clean_items,
            "raw_source": raw,
            "delivery_type": "delivery" if dlv_addr else "balcao",
            "notes": raw.get('observations', '')
        }
        
        
def process_ifood_update(adapter: IfoodAdapter, order: Order, status: str, old_status: str):
    """Lógica isolada para atualizações do iFood"""
    try:
        pm_lower = (order.payment_method or "").lower()
        if "ifood" not in pm_lower: return

        if status == 'PREPARO' and old_status == 'PENDENTE':
            adapter.confirm_order(order.external_id)
        elif status == 'SAIU_ENTREGA':
            adapter.dispatch_order(order.external_id)
        elif status == 'PRONTO' and 'Retirada' in (order.address_street or ""):
            adapter.ready_to_pickup(order.external_id)
            
    except Exception as e:
        print(f"⚠️ Erro Lógica iFood: {e}")