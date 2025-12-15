import json
import re

def normalize_order_items_for_view(order):
    if not order.items_json:
        return []

    raw_items = order.items_json
    
    # === REGRA DE OURO: A ESTRUTURA MANDA MAIS QUE A ORIGEM ===
    # Se encontrarmos indícios de que o pedido foi salvo/editado pelo sistema interno (PDV),
    # usamos o tradutor do PDV imediatamente, ignorando se é Wabiz ou iFood.
    
    is_internal_format = False
    
    if isinstance(raw_items, list):
        for item in raw_items:
            # 1. Tem parts_rich? (Assinatura clara do novo PDV)
            if item.get('parts_rich'):
                is_internal_format = True
                break
            
            # 2. Tem parts e é uma lista? (Assinatura do PDV legado/novo)
            # A Wabiz usa 'products' ou 'display_lines', nunca 'parts' como lista principal de sabores
            if item.get('parts') and isinstance(item['parts'], list) and len(item['parts']) > 0:
                is_internal_format = True
                break
                
            # 3. Tem kds_stage? (Só o sistema interno adiciona isso)
            if item.get('kds_stage') is not None:
                is_internal_format = True
                break
            
            # 4. (NOVO) Tem 'details' preenchido? Então é formato interno válido!
            if item.get('details') and isinstance(item['details'], list) and len(item['details']) > 0:
                is_internal_format = True
                break

    if is_internal_format:
        try:
            return _translate_pdv(raw_items)
        except Exception as e:
            print(f"⚠️ Erro no Tradutor PDV (Forçado): {e}")
            # Se der erro, tentamos seguir o fluxo normal abaixo, mas é improvável

    # === SE NÃO É FORMATO INTERNO, TENTA DESCOBRIR A ORIGEM ===
    
    platform = "PDV" 
    pm = (order.payment_method or "").lower()
    wabiz_id = str(order.wabiz_id or "")
    ext_id = str(order.external_id or "").lower()

    if "ifood" in pm or "ifood" in ext_id:
        platform = "IFOOD"
    elif wabiz_id and not wabiz_id.startswith("M-"):
        platform = "WABIZ"
    
    try:
        # Só usa os tradutores externos se tiver certeza que a estrutura NÃO é interna
        if platform == "IFOOD":
            return _translate_ifood(raw_items)
        elif platform == "WABIZ":
            return _translate_wabiz(raw_items)
        else:
            return _translate_pdv(raw_items)
    except Exception as e:
        print(f"❌ Erro Normalizer ({platform}): {e}")
        return _translate_pdv(raw_items)
    

def _inject_metadata(source, target):
    # BLINDAGEM: Garante que kds_stage seja sempre um inteiro válido
    try:
        val = source.get('kds_stage')
        if val is None or val == "":
            target['kds_stage'] = 0
        else:
            target['kds_stage'] = int(val)
    except:
        target['kds_stage'] = 0 # Fallback seguro
    
    target['kds_done'] = source.get('kds_done', False)
    target['external_code'] = source.get('external_code') or source.get('id')
    return target

def _detail(text, code=None, type='info'):
    return {"text": text, "code": code, "type": type}

# ==========================================
#           TRADUTOR 1: WABIZ
# ==========================================
def _translate_wabiz(items):
    standardized = []
    
    for item in items:
        price = float(item.get('price', item.get('unitPrice', 0))) # <--- CAPTURA PREÇO

        # A) Formato do Banco (Display Lines - Mais Rico)
        if item.get('display_lines'):
            name = item.get('title') or item.get('name') or "Item"
            name = name.replace('COMBO:', '').replace('PIZZAS FAMÍLIA - ', '').replace('PIZZAS GRANDES - ', '').strip()
            qty = float(item.get('quantity', 1))
            
            details = []
            edges = []
            
            for line in item['display_lines']:
                txt = line.get('text', '')
                code = line.get('code')
                l_type = line.get('type', 'info')
                
                if l_type == 'edge':
                    if '🧀' not in txt: txt = f"🧀 {txt}"
                    edges.append(_detail(txt, code, 'edge'))
                elif l_type == 'flavor':
                    details.append(_detail(f"• {txt}", code, 'flavor'))
                elif l_type == 'addon':
                    details.append(_detail(f"  + {txt.replace('+ ', '')}", code, 'addon'))
                elif l_type == 'info' and 'Item do Combo' not in txt:
                    details.append(_detail(f"• {txt}", code, 'info'))

            final_details = details + edges
            
            obs_lines = [l['text'] for l in item['display_lines'] if l.get('type') == 'obs']
            obs = " / ".join(obs_lines)

            final_item = {
                "quantity": qty,
                "name": name,
                "price": price, # <--- PRESERVA PREÇO
                "details": final_details,
                "observation": obs,
                "removed": [],
                "is_pizza": "PIZZA" in name.upper() or len(edges) > 0
            }
            standardized.append(_inject_metadata(item, final_item))

        # B) Formato Webhook (Bruto)
        elif 'products' in item:
            for prod in item['products']:
                qty = float(prod.get('qty', 1))
                prod_price = float(prod.get('price', 0)) # Preço do sub-item
                parts = prod.get('parts', [])
                
                is_pizza = False
                if len(parts) > 1:
                    main_name = " / ".join([p.get('name', 'Sabor') for p in parts])
                    is_pizza = True
                elif len(parts) == 1:
                    main_name = parts[0].get('name', 'Item')
                    group_name = item.get('groupName', '').upper()
                    is_pizza = "PIZZA" in group_name or "PIZZA" in main_name.upper()
                else:
                    main_name = prod.get('name', 'Item')

                details = []
                for part in parts:
                    p_code = part.get('externalCode')
                    cust = part.get('customization', {})
                    
                    if len(parts) > 1:
                         details.append(_detail(f"• {part.get('name')}", p_code, 'flavor'))

                    for grp in cust.get('additionals', []):
                        for opt in grp.get('options', []):
                            if opt.get('name'): 
                                details.append(_detail(f"+ {opt['name']}", opt.get('externalCode'), 'addon'))
                    
                    edge_grp = cust.get('edge', {})
                    if isinstance(edge_grp, dict): 
                        for opt in edge_grp.get('options', []):
                            if opt.get('name'): 
                                details.append(_detail(f"🧀 Borda: {opt['name']}", opt.get('externalCode'), 'edge'))
                    
                    for grp in cust.get('combo', []):
                        for opt in grp.get('options', []):
                            if opt.get('name'): 
                                details.append(_detail(f"• {opt['name']}", opt.get('externalCode'), 'info'))

                obs = item.get('obs')
                if parts and parts[0].get('obs'): obs = parts[0].get('obs')

                final_item = {
                    "quantity": qty,
                    "name": main_name,
                    "price": prod_price, # <--- PRESERVA PREÇO
                    "details": details,
                    "observation": obs or "",
                    "removed": [],
                    "is_pizza": is_pizza
                }
                standardized.append(_inject_metadata(prod, final_item))

        # C) Fallback
        else:
            name = item.get('name') or item.get('title') or "Item"
            qty = float(item.get('quantity') or 1)
            details = []
            sub_items = item.get('subItems') or item.get('components') or []
            for sub in sub_items: 
                details.append(_detail(f"• {sub.get('name', '')}", sub.get('externalCode')))
            
            final_item = {
                "quantity": qty,
                "name": name,
                "price": price, # <--- PRESERVA PREÇO
                "details": details,
                "observation": item.get('observation') or "",
                "removed": [],
                "is_pizza": "PIZZA" in name.upper()
            }
            standardized.append(_inject_metadata(item, final_item))

    return standardized

# ==========================================
#           TRADUTOR 2: IFOOD
# ==========================================

def _translate_ifood(items):
    standardized = []
    for item in items:
        name = item.get('title') or item.get('name') or "Item"
        price = float(item.get('unitPrice', item.get('price', 0))) 
        details = []
        
        # Pega lista de nível 2
        addons = item.get('addons') or item.get('options') or item.get('subItems') or []
        
        for opt in addons:
            opt_name = opt.get('name', '')
            opt_code = opt.get('externalCode') or opt.get('pdvId')
            if not opt_name: continue
            
            try: qty_opt = float(opt.get('quantity', 1))
            except: qty_opt = 1.0
            
            prefix_qty = f"{int(qty_opt)}x " if qty_opt > 1 else ""

            # --- EXIBE NÍVEL 2 (O Complemento Pai) ---
            # Tenta limpar visualmente caso venha "Nome (Detalhe)" antigo
            display_name = opt_name
            if '(' in opt_name and opt_name.strip().endswith(')'):
                 display_name = opt_name.split('(', 1)[0].strip()

            # Adiciona o Nível 2 (Verde/Adicional)
            details.append(_detail(f"+ {prefix_qty}{display_name}", opt_code, 'addon'))

            # --- AQUI ESTÁ A CORREÇÃO: NÍVEL 3 (Sub-Itens) ---
            # Lê a lista 'sub_items' que o seu JSON já tem
            sub_items = opt.get('sub_items') or opt.get('subItems') or []
            
            for sub in sub_items:
                sub_name = sub.get('name', '')
                try: sub_qty = float(sub.get('quantity', 1))
                except: sub_qty = 1.0
                
                sub_prefix = f"{int(sub_qty)}x " if sub_qty > 1 else ""
                
                # Adiciona visualmente recuado (Nível 3)
                if sub_name:
                    details.append(_detail(f"    • {sub_prefix}{sub_name}", sub.get('external_code'), 'info'))
            # -----------------------------------------------------

        obs = item.get('notes') or item.get('observations') or ""
        final_item = {
            "quantity": item.get('quantity', 1),
            "name": name,
            "price": price,
            "details": details, # Agora inclui o nível 3
            "observation": obs,
            "removed": [], 
            "is_pizza": "pizza" in name.lower()
        }
        standardized.append(_inject_metadata(item, final_item))
    return standardized

# ==========================================
#           TRADUTOR 3: PDV (FINAL)
# ==========================================
def _translate_pdv(items):
    standardized = []
    for item in items:
        # Pega o Título e limpa prefixos
        raw_title = item.get('title') or item.get('name') or "Item"
        if raw_title.upper().startswith("PIZZA PIZZA"): raw_title = raw_title[6:].strip()
        
        price = float(item.get('price', 0))
        name = raw_title
        details = []
        
        # Identificação de IDs
        prod_id_internal = str(item.get('product_id') or "")
        ext_code = str(item.get('external_code') or "")
        main_code = prod_id_internal if prod_id_internal else (ext_code if ext_code else str(item.get('id') or ""))

        # ==============================================================================
        # 1. PRIORIDADE TOTAL: SE TEM DETAILS PRONTOS (Visualização Fiel)
        # ==============================================================================
        # Se o item já tem os detalhes processados (seja da Wabiz, iFood ou de uma edição anterior),
        # usamos eles! Isso preserva os adicionais (+ Bacon) que sumiriam se recalculássemos apenas pelo 'parts'.
        if item.get('details') and isinstance(item['details'], list) and len(item['details']) > 0:
             details = item['details']
             
             # Limpa a "tripa" do título se ela ainda estiver lá, pois agora temos os detalhes bonitos
             if ':' in name:
                 name = name.split(':')[0].strip()
                 
        # --- INICIO DA CORREÇÃO ---
        elif item.get('display_lines') and isinstance(item['display_lines'], list) and len(item['display_lines']) > 0:
             # Recupera dados legados que vieram como display_lines (Wabiz/Promoções)
             details = item['display_lines']
        # --- FIM DA CORREÇÃO ---

        # ==============================================================================
        # 2. SE NÃO TEM DETAILS, RECALCULA (Criação PDV / Edição que mudou item)
        # ==============================================================================
        else:
            # Lógica de recuperação baseada em 'parts' ou 'parts_rich'
            has_source_data = (item.get('parts') and len(item['parts']) > 0) or (item.get('parts_rich') and len(item['parts_rich']) > 0)
            
            if has_source_data:
                edge_detail = None
                
                # --- A: PIZZA (Com parts_rich) ---
                if item.get('parts_rich'):
                    if ':' in raw_title:
                        parts_title = raw_title.split(':', 1)
                        name = parts_title[0].strip()
                        if '(Borda:' in parts_title[1]:
                            try:
                                edge_txt = parts_title[1].split('(Borda:')[1].split(')')[0].strip()
                                edge_detail = _detail(f"🧀 Borda: {edge_txt}", None, 'edge')
                            except: pass

                    for part_obj in item['parts_rich']:
                        p_name = part_obj.get('name', '')
                        p_id = str(part_obj.get('id', ''))
                        
                        if '(+' in p_name:
                            try:
                                flavor, extras_raw = p_name.split('(+')
                                flavor = flavor.strip()
                                details.append(_detail(f"• {flavor}", p_id, 'flavor'))
                                
                                extras_raw = extras_raw.replace(')', '').strip()
                                for extra in extras_raw.split(','):
                                    details.append(_detail(f"  + {extra.strip()}", None, 'addon'))
                            except:
                                details.append(_detail(f"• {p_name}", p_id, 'flavor'))
                        else:
                            details.append(_detail(f"• {p_name}", p_id, 'flavor'))

                    if edge_detail: details.append(edge_detail)

                # --- B: PIZZA LEGADO (Só strings) ---
                elif item.get('parts'):
                    for part in item['parts']:
                        details.append(_detail(f"• {part}", None, 'flavor')) 
                        
                # --- C: Limpeza de Texto (Tripa no Título) ---
                if not item.get('parts_rich') and not item.get('parts') and '(' in raw_title:
                    try:
                        parts = raw_title.split('(', 1)
                        name = parts[0].strip()
                        rest = parts[1].rsplit(')', 1)[0]
                        sub_items = [s.strip() for s in rest.split(',')]
                        for sub in sub_items:
                            if sub.lower().startswith('obs:'): continue
                            dtype = 'info'
                            if sub.startswith('+'): dtype = 'addon'
                            elif sub.lower().startswith('sem '): dtype = 'removed'
                            elif 'borda' in sub.lower(): dtype = 'edge'
                            details.append(_detail(sub, None, dtype))
                    except: pass

            # --- D: COMBO ---
            elif item.get('combo_data'):
                if '(' in raw_title: name = raw_title.split('(', 1)[0].strip()
                for group in item['combo_data']:
                    for sub in group.get('items', []):
                        qty = sub.get('qty', 0)
                        if qty > 0: 
                            details.append(_detail(f"{qty}x {sub['name']}", sub.get('id'), 'info'))

        final_item = {
            "quantity": item.get('quantity', 1),
            "name": name,
            "price": price,
            "details": details, # Agora garantido!
            "observation": item.get('observation', ""),
            "removed": item.get('removed_names', []),
            "is_pizza": item.get('type') == 'pizza' or len(details) > 0 
        }
        
        if prod_id_internal:
            final_item['id'] = prod_id_internal
            final_item['product_id'] = prod_id_internal
            final_item['external_code'] = None 
        else:
            final_item['external_code'] = main_code
            final_item['id'] = main_code
        
        standardized.append(_inject_metadata(item, final_item))
        
    return standardized