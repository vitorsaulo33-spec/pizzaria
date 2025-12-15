import requests
import re
import pytz
import json
from typing import List, Dict, Any
from datetime import datetime
from collections import Counter
from .base import BaseIntegration
from models import Order
from fastapi import status

class WabizAdapter(BaseIntegration):
    
    def _get_auth_token(self):
        try:
            url = f"{self.base_url.rstrip('/')}/token"
            
            payload = {
                "grant_type": "password",
                "username": self.user,
                "password": self.password
            }
            
            # 👇 ADICIONE ESTA LINHA AQUI (Ela cria a variável que estava faltando)
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            
            # Agora a variável 'headers' existe e o VS Code não vai mais reclamar
            r = requests.post(url, data=payload, headers=headers, timeout=15)
            
            if r.status_code == 200:
                token = r.json().get("access_token")
                return token
            
            print(f"❌ [Wabiz] Erro Auth ({r.status_code}): {r.text}")
            return None
            
        except Exception as e:
            print(f"❌ [Wabiz] Erro Conexão: {e}")
            return None

   # --- 2. BUSCA DE PEDIDOS (VERSÃO 2) ---
    def fetch_orders(self) -> List[Dict[str, Any]]:
        token = self._get_auth_token()
        if not token: return []

        try:
            # CORREÇÃO: O endpoint de pedidos mudou para V2
            # A base_url padrão termina em /api/v1, então trocamos para /api/v2
            base_v2 = self.base_url.replace("/api/v1", "/api/v2")
            url = f"{base_v2.rstrip('/')}/orders/pending"
            
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            
            # Timeout de 15s para garantir
            resp = requests.get(url, headers=headers, timeout=15)
            
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list): return data
                return data.get("data", []) or []
            
            print(f"⚠️ [Wabiz] Erro ao buscar pedidos ({resp.status_code}): {url}")
            return []
        except Exception as e:
            print(f"❌ [Wabiz] Erro Fetch: {e}")
            return []
        

    def normalize_order(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        print(f"🛑 [WABIZ] Processando #{raw.get('orderNumber')} (Doc Oficial)")

        # GRAVAÇÃO EM UTC (Padrão Mundial)
        # O banco vai receber ex: 22:00 (que equivale a 19:00 BR)
        # O KDS vai ler 22:00, converter pra BR e mostrar 19:00. A conta fecha.
        created_at_utc = datetime.utcnow()
        
        # 1. Cliente
        cust = raw.get('customer', {})
        phone = self._normalize_phone(cust.get('phoneNumber'), cust.get('phoneCode'))
        
        # 2. Identificação do Serviço (Baseado na Doc)
        service = raw.get('service', {})
        svc_type = service.get('type', 'delivery') # delivery, pickup, internal_delivery, etc
        
        # Estrutura de dados de entrega vazia por padrão
        dlv_data = {}
        is_takeout = False
        
        # LÓGICA DE EXTRAÇÃO BASEADA NO TIPO
        if svc_type in ['delivery', 'scheduleOrder_delivery']:
            # Padrão Delivery: Objeto 'delivery'
            dlv_data = service.get('delivery', {})
            # Fallback: Algumas versões jogam na raiz se o service vier vazio
            if not dlv_data and 'delivery' in raw: dlv_data = raw['delivery']
            
        elif svc_type == 'internal_delivery':
            # Padrão Interno: Objeto 'internalDelivery'
            dlv_data = service.get('internalDelivery', {})
            # A doc diz que tem 'info' (local) ao invés de endereço completo
            if 'info' in dlv_data:
                dlv_data['address'] = dlv_data.get('info')
                dlv_data['region'] = 'Entrega Interna'

        elif svc_type in ['pickup', 'scheduleOrder_pickup', 'table']:
            is_takeout = True
        
        # 3. Mapeamento Estrito dos Campos (Conforme Doc)
        # address: Logradouro
        # number: Número
        # region: Bairro (CRÍTICO: O código antigo buscava neighborhood)
        # postalCode: CEP
        # tax: Taxa de Entrega (CRÍTICO: O código antigo buscava fee)
        
        final_street = "Retirada" if is_takeout else (dlv_data.get('address') or "")
        number = dlv_data.get('number') or ""
        # Aqui garantimos que pegamos 'region' conforme a doc
        neighborhood = dlv_data.get('region') or dlv_data.get('neighborhood') or "" 
        
        try: 
            # Doc diz 'tax' (double)
            delivery_fee = float(dlv_data.get('tax') or dlv_data.get('fee') or 0.0)
        except: 
            delivery_fee = 0.0

        # IDs
        display_id = str(raw.get('orderNumber') or raw.get('id'))
        external_id = str(raw.get('internalKey') or raw.get('id'))
        
        # 4. Processamento de Itens
        clean_items = self._parse_items_standard(raw.get('items', []))
        
        # 5. Totais e Validações
        items_total = sum((item['quantity'] * item['price']) for item in clean_items)
            
        try: raw_total_api = float(raw.get('total') or raw.get('payment', {}).get('total') or 0.0)
        except: raw_total_api = 0.0
        
        # --- CORREÇÃO: Captura de Desconto ---
        try: 
            discount = float(raw.get('discount') or raw.get('payment', {}).get('discount') or 0.0)
        except: 
            discount = 0.0

        if raw_total_api > 0:
            final_total = raw_total_api
            
            # Cálculo esperado (Itens + Entrega)
            expected_total = items_total + delivery_fee
            
            # Se o total final for MENOR que o esperado, a diferença é Desconto (se não veio no JSON)
            if discount == 0 and final_total < expected_total:
                diff = expected_total - final_total
                if diff > 0.01: # Margem de erro de centavos
                    discount = diff
            
            # Se o total final for MAIOR que o esperado, a diferença é Taxa (Lógica antiga mantida)
            elif delivery_fee == 0 and final_total > expected_total:
                diff = final_total - items_total # Usa items_total pois delivery_fee é 0
                if diff < 50: delivery_fee = diff
                
        else:
            # Se não veio total, calcula somando
            final_total = items_total + delivery_fee - discount

        payment_info = dlv_data.get('payment', {})
        # Se não achou no delivery, tenta na raiz (algumas versões)
        if not payment_info: payment_info = raw.get('payment', {})
        
        payment_name = payment_info.get('name', 'Outros')
        
        # Mapeamento de Tipos Numéricos da Doc
        pay_type = payment_info.get('type')
        if pay_type == 1 and payment_name == 'Outros': payment_name = 'Dinheiro'
        if pay_type == 7 and payment_name == 'Outros': payment_name = 'Pix'

        if payment_info.get('cardFlag'): 
            payment_name += f" ({payment_info.get('cardFlag')})"

        # --- NOVA LÓGICA DE TROCO (SOLICITADO) ---
        try:
            # Pega o valor que o cliente disse que vai pagar (ex: 50.00)
            amount_paid = float(payment_info.get('value', 0.0))
            
            # Se for dinheiro e o valor pago for maior que o total do pedido
            if "Dinheiro" in payment_name and amount_paid > final_total:
                change_val = amount_paid - final_total
                if change_val > 0:
                    # Formata exatamente como o seu sistema espera para exibir o alerta amarelo
                    payment_name += f" (Troco p/ {amount_paid:.2f} -> R$ {change_val:.2f})"
        except Exception as e:
            print(f"⚠️ [Wabiz] Erro ao calcular troco: {e}")
        # -----------------------------------------

        return {
            # ... (o retorno continua igual) ...
            "external_id": external_id,
            "display_id": display_id,
            "total_value": float(final_total),
            "delivery_fee": float(delivery_fee),
            "created_at": created_at_utc,
            "customer": {
                "name": cust.get('name', 'Cliente Wabiz'),
                "phone": phone,
                "email": cust.get('email')
            },
            "address": {
                "street": final_street,
                "number": number,
                "neighborhood": neighborhood,
                "city": dlv_data.get('city'),
                "state": dlv_data.get('state'),
                "zip_code": dlv_data.get('postalCode'),
                "complement": dlv_data.get('compl')
            },
            "payment_method": payment_name, # <--- Agora leva a string do troco junto
            "items": clean_items,
            "raw_source": raw,
            "notes": raw.get('obs'),
            "integration_source": "wabiz",
            "delivery_type": "balcao" if is_takeout else "delivery"
        }

    # --- NOVO PARSER PADRONIZADO (O CÉREBRO) ---
    def _parse_items_standard(self, raw_items_list):
        standard_items = []
        for group in raw_items_list:
            group_name = str(group.get('groupName', '')).strip().replace("PIZZA PIZZA", "PIZZA")
            is_quantity_combo = "COMBOS ESFIHAS" in group_name.upper() or "ESFIHA" in group_name.upper()
            
            products = group.get('products', [])
            if not isinstance(products, list): continue

            for p in products:
                try: qty = float(p.get('qty', 1))
                except: qty = 1.0
                try: price = float(p.get('price', 0))
                except: price = 0.0
                
                parts = p.get('parts', [])
                name = p.get('name', 'Item')
                
                # --- CORREÇÃO DE ID (Wabiz V2) ---
                # Tenta pegar o código no item pai. Se não tiver, pega da primeira parte (Onde o Combo esconde o ID)
                main_code = str(p.get('externalCode') or p.get('code') or '')
                if not main_code and parts:
                    main_code = str(parts[0].get('externalCode') or parts[0].get('code') or '')
                # ---------------------------------

                raw_name = self._clean_name(name)
                combo_display = raw_name if raw_name and raw_name != 'Item' else group_name
                
                if is_quantity_combo and parts:
                    first = parts[0].get('name', '')
                    if first: combo_display = self._clean_name(first)

                flavor_parts = []
                upsell_items = []
                
                # CORREÇÃO: Dicionário para agrupar por nome SOMANDO as quantidades
                combo_map = {} 

                for part in parts:
                    part_name = self._clean_name(part.get('name', ''))
                    part_code = str(part.get('externalCode') or '')
                    
                    is_main_flavor = bool(part_name)
                    
                    this_flavor_extras = []
                    this_flavor_removed = []
                    
                    cust = part.get('customization') or {}
                    
                    if 'combo' in cust:
                        for grp in cust['combo']:
                            # --- CORREÇÃO: Loop manual para ler profundidade (Nível 3) ---
                            for raw_opt in grp.get('options', []):
                                # 1. Dados Básicos
                                opt_name = self._clean_name(raw_opt.get('name', ''))
                                opt_code = str(raw_opt.get('externalCode') or raw_opt.get('code') or raw_opt.get('productId') or raw_opt.get('id') or '')
                                try:
                                    opt_qty = float(raw_opt.get('amount', 0))
                                    if opt_qty <= 0: opt_qty = float(raw_opt.get('qty', 0))
                                    if opt_qty <= 0: opt_qty = 1.0
                                except: opt_qty = 1.0

                                # --- NOVA LÓGICA: CAPTURA UNIVERSAL DE DETALHES ---
                                sub_details = []
                                sub_cust = raw_opt.get('customization', {})
                                
                                # A) Adicionais (ex: Catupiry, Cheddar)
                                if 'additionals' in sub_cust:
                                    for s_grp in sub_cust['additionals']:
                                        for s_opt in s_grp.get('options', []):
                                            s_name = s_opt.get('name', '').strip()
                                            if s_name: sub_details.append(f"+ {s_name}")
                                
                                # B) Observação do Item
                                if raw_opt.get('obs'):
                                    sub_details.append(f"Obs: {raw_opt.get('obs')}")
                                # --------------------------------------------------

                                # 2. Lógica Específica
                                if is_quantity_combo:
                                    # C) Cria Chave Única para agrupar (Esfihas de quantidade)
                                    unique_key = f"{opt_name}||{'|'.join(sorted(sub_details))}"

                                    if unique_key not in combo_map:
                                        combo_map[unique_key] = {
                                            'name': opt_name, 
                                            'qty': 0.0, 
                                            'code': opt_code,
                                            'details_list': sub_details 
                                        }
                                    combo_map[unique_key]['qty'] += opt_qty
                                
                                else:
                                    # Mantém o fluxo normal para Upsells, MAS AGORA SALVA OS DETALHES
                                    upsell_items.append({
                                        "name": opt_name, 
                                        "price": float(raw_opt.get('price', 0)), 
                                        "code": opt_code, 
                                        "qty": opt_qty,
                                        "details": sub_details # <--- CAMPO NOVO IMPORTANTE
                                    })

                    if 'additionals' in cust:
                        for grp in cust['additionals']:
                            for opt in self._extract_options_flat(grp.get('options', [])):
                                if "sem " in opt['name'].lower(): this_flavor_removed.append(opt['name'])
                                else: this_flavor_extras.append(opt['name'])

                    if 'edge' in cust and cust['edge']:
                        for opt in self._extract_options_flat(cust['edge'].get('options', [])):
                            this_flavor_extras.append(f"Borda: {opt['name']}")

                    if 'others' in cust:
                        for grp in cust['others']:
                            for opt in self._extract_options_flat(grp.get('options', [])):
                                this_flavor_extras.append(opt['name'])

                    if is_main_flavor:
                        flavor_parts.append({
                            "name": part_name,
                            "code": part_code, # Guarda código para o link da Pizza
                            "extras": this_flavor_extras,
                            "removed": this_flavor_removed,
                            "obs": part.get('obs')
                        })

                # CASO A: COMBO DE QUANTIDADE (Esfihas)
                if is_quantity_combo and combo_map:
                    standard_items.append({
                        "title": f"COMBO: {combo_display}",
                        "quantity": qty,
                        "price": price,
                        "display_lines": [],
                        "is_header": True,
                        "external_code": main_code
                    })
                    
                    # Gera itens filhos usando o mapa somado
                    # ALTERAÇÃO: Usamos _ pois a chave não é necessária aqui, tudo está em 'data'
                    for _, data in combo_map.items(): 
                        total_q = data['qty'] * qty
                        
                        # --- CORREÇÃO: Monta as linhas visuais (Verde/Branco no KDS) ---
                        lines = [{"text": "Item do Combo", "type": "info"}]
                        
                        # Adiciona cada detalhe capturado (Catupiry, Obs, etc)
                        for d in data['details_list']:
                            d_type = 'addon' if d.startswith('+') else 'obs'
                            lines.append({"text": d, "type": d_type})

                        standard_items.append({
                            "title": data['name'],
                            "quantity": total_q,
                            "price": 0.0,
                            "display_lines": lines, # <--- Enviando os detalhes para o frontend
                            "is_header": False,
                            "external_code": data['code']
                        })

                # CASO B: PIZZA / ITEM NORMAL
                else:
                    upsell_val = sum(u['price'] for u in upsell_items)
                    if upsell_val > 0: price = max(0.0, price - upsell_val)

                    final_title = combo_display
                    if flavor_parts: final_title = group_name

                    display_lines = []
                    total_flavors = len(flavor_parts)
                    
                    for idx, fp in enumerate(flavor_parts):
                        fraction = f"1/{total_flavors}" if total_flavors > 1 else ""
                        if total_flavors == 2: fraction = "½"
                        
                        text = f"{fraction} {fp['name']}".strip()
                        # Passa o código do sabor (ex: Calabresa) para o frontend
                        display_lines.append({"text": text, "type": "flavor", "code": fp['code']})
                        
                        for ext in fp['extras']:
                            if "Borda:" in ext: display_lines.append({"text": ext, "type": "edge"})
                            else: display_lines.append({"text": f"+ {ext}", "type": "addon"})
                        
                        for rem in fp['removed']:
                            display_lines.append({"text": f"Sem: {rem}", "type": "removed"})
                        
                        if fp['obs']:
                            display_lines.append({"text": f"Obs: {fp['obs']}", "type": "obs"})

                    if p.get('obs'):
                         display_lines.append({"text": f"Obs Geral: {p.get('obs')}", "type": "obs"})

                    standard_items.append({
                        "title": final_title,
                        "quantity": qty,
                        "price": price,
                        "display_lines": display_lines,
                        "is_header": False,
                        "external_code": main_code
                    })

                # CASO C: UPSELLS (E Itens de Combo Genéricos / Esfihas)
                for up in upsell_items:
                    # Inicia a lista de linhas visuais
                    u_lines = []
                    
                    # 1. Se tiver detalhes capturados (Adicionais/Obs), adiciona eles
                    if up.get('details') and len(up['details']) > 0:
                        # Adiciona o marcador de item do combo primeiro
                        u_lines.append({"text": "Item do Combo", "type": "info"})
                        
                        for d_txt in up['details']:
                            # Define se é Obs (Amarelo) ou Adicional (Verde)
                            d_type = 'obs' if 'Obs:' in d_txt else 'addon'
                            u_lines.append({"text": d_txt, "type": d_type})
                    
                    # 2. Se NÃO tiver detalhes, mas estiver dentro de um grupo (combo), avisa que é item do combo
                    elif is_quantity_combo or 'COMBO' in group_name.upper():
                        u_lines.append({"text": "Item do Combo", "type": "info"})

                    standard_items.append({
                        "title": up['name'],
                        "quantity": up['qty'] * qty, # Multiplica pela qtd do combo pai
                        "price": up['price'],
                        "display_lines": u_lines, # <--- Agora envia a lista correta
                        "is_header": False,
                        "external_code": up['code']
                    })

        return standard_items

 # --- AUXILIARES (Substitua o final do arquivo por isso) ---
    def _normalize_phone(self, raw_phone, raw_ddd): 
        if not raw_phone: return ""
        return f"55{raw_ddd}{raw_phone}"
        
    def _clean_name(self, name): 
        return re.sub(r'^\(\d+\)\s*', '', str(name)).replace("PIZZA PIZZA", "PIZZA").strip()
        
    def _extract_options_flat(self, opt_list): 
        results = []
        for o in opt_list:
            # Lógica de quantidade
            try:
                qty = float(o.get('amount', 0))
                if qty <= 0: qty = float(o.get('qty', 0))
                if qty <= 0: qty = 1.0
            except:
                qty = 1.0

            # CORREÇÃO: Busca o código em múltiplos campos para evitar criação de duplicatas
            # O JSON da Wabiz varia entre 'externalCode', 'code', 'id' ou 'productId'
            raw_code = o.get('externalCode') or o.get('code') or o.get('productId') or o.get('id') or ''

            results.append({
                "name": self._clean_name(o.get('name','')), 
                "price": float(o.get('price',0)), 
                "code": str(raw_code), 
                "qty": qty 
            })
        return results


# --- ENVIO DE STATUS (CONFIRMAÇÃO / DESPACHO) ---
    def update_status(self, order_number, internal_key, status_code, message=""):
        token = self._get_auth_token()
        if not token: return False
        
        # A URL é V1 conforme sua documentação
        url = f"{self.base_url.replace('/api/v2', '/api/v1').rstrip('/')}/orders/status"
        
        payload = {
            "orderNumber": order_number,
            "internalKey": internal_key,
            "newOrderStatus": status_code,
            "sendNotification": "true",
            "additionalMessage": message,
            "isProcessed": "true"
        }
        
        headers = {
            "Authorization": f"Bearer {token}", 
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json"
        }
        
        try:
            print(f"📤 [Wabiz] Atualizando Status #{order_number} -> {status_code}...")
            r = requests.post(url, data=payload, headers=headers, timeout=10)
            
            if r.status_code == 200:
                print(f"✅ [Wabiz] Sucesso: {r.text}")
                return True
            else:
                print(f"⚠️ [Wabiz] Falha ({r.status_code}): {r.text}")
                return False
        except Exception as e:
            print(f"❌ [Wabiz] Erro Conexão: {e}")
            return False
        
    
    # --- INSIRA ISTO DENTRO DA CLASSE WabizAdapter ---
    def get_order_by_id(self, order_number: str) -> Dict[str, Any]:
        """Busca pedido específico para resgate imediato (Race Condition)"""
        token = self._get_auth_token()
        if not token: return None

        try:
            # Garante uso da V2
            base_v2 = self.base_url.replace("/api/v1", "/api/v2")
            url = f"{base_v2.rstrip('/')}/orders/{order_number}"
            
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            
            # Timeout curto pois é uma operação de tempo real
            resp = requests.get(url, headers=headers, timeout=10)
            
            if resp.status_code == 200:
                data = resp.json()
                # A Wabiz às vezes retorna o objeto direto, às vezes dentro de 'data'
                return data.get("data", data)
            
            return None
        except Exception as e:
            print(f"❌ [Wabiz] Erro ao resgatar pedido {order_number}: {e}")
            return None
    
        
def process_wabiz_update(adapter: 'WabizAdapter', order: Order, status: str):
    """
    TRADUÇÃO DE STATUS (SISTEMA INTERNO -> WABIZ)
    Versão Blindada: Detecta Balcão pelo tipo OU pelo endereço.
    """
    """
    TRADUÇÃO DE STATUS (SISTEMA INTERNO -> WABIZ)
    
    Mapeamento Oficial:
    1: Não Confirmado
    2: Confirmado
    3: Em Produção
    4: Pronto para retirar (Exclusivo Balcão/Retirada)
    5: Saiu para entrega (Exclusivo Delivery)
    6: Finalizado
    7: Cancelado
    """
    try:
        if not order.wabiz_id or str(order.wabiz_id).startswith("M-"): return
        
        pm_lower = (order.payment_method or "").lower()
        if "ifood" in pm_lower: return

        wb_code = None
        wb_msg = ""
        s_upper = status.upper().strip()
        
        # --- CORREÇÃO AQUI: DETECÇÃO ROBUSTA DE BALCÃO ---
        # Verifica se está marcado como balcão OU se o endereço diz "Retirada"
        is_balcao = (
            str(order.delivery_type or '').lower() == 'balcao' or 
            "retirada" in str(order.address_street or '').lower()
        )

        # 2. CONFIRMADO
        if s_upper in ['NOVO', 'PENDENTE', 'AGUARDANDO', 'CONFIRMADO']:
            wb_code = 2
            wb_msg = "Pedido recebido e confirmado!"

        # 3. EM PRODUÇÃO
        elif s_upper in ['EM_PREPARO', 'PREPARO', 'ACEITO', 'FORNO']:
            wb_code = 3
            wb_msg = "Seu pedido está sendo preparado!"

        # 4. PRONTO (O Ponto Crítico)
        elif s_upper in ['PRONTO', 'EXPEDICAO']: 
            if is_balcao:
                wb_code = 4
                wb_msg = "Seu pedido está pronto para retirada!"
                print(f"👉 [Wabiz] Pedido Balcão #{order.wabiz_id} -> Status 4 (Pronto)")
            else:
                # Se for Delivery, PRONTO ainda não avisa o cliente (só quando sair)
                return 

        # 5. SAIU PARA ENTREGA
        elif s_upper == 'SAIU_ENTREGA':
            wb_code = 5
            wb_msg = "Seu pedido saiu para entrega!"

        # 6. FINALIZADO
        elif s_upper in ['ENTREGUE', 'CONCLUIDO']:
            wb_code = 6
            wb_msg = "Pedido finalizado."

        # 7. CANCELADO
        elif 'CANCELADO' in s_upper:
            wb_code = 7
            wb_msg = "Pedido cancelado."

        if wb_code and order.external_id:
            result = adapter.update_status(order.wabiz_id, order.external_id, wb_code, wb_msg)
            if result:
                print(f"✅ [Wabiz] Status atualizado: {wb_code}")

    except Exception as e:
        print(f"⚠️ Erro Lógica Wabiz: {e}")
    
