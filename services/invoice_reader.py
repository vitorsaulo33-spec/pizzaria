import google.generativeai as genai
import json
import os
import time
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
MODEL_GEMINI = 'gemini-2.0-flash'

def parse_xml_nfe(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes)
        items_found = []
        invoice_key = None
        total_invoice_value = 0.0

        # Namespaces comuns da NFe
        ns = {'nfe': 'http://www.portalfiscal.inf.br/nfe'}

        # 1. Tenta extrair a Chave de Acesso
        for elem in root.iter():
            if 'chNFe' in elem.tag:
                invoice_key = elem.text
                break
        
        if not invoice_key:
            inf_nfe = root.find('.//nfe:infNFe', ns)
            if inf_nfe is not None and 'Id' in inf_nfe.attrib:
                invoice_key = inf_nfe.attrib['Id'].replace('NFe', '')

        # 2. Tenta extrair o Valor Total da Nota (vNF)
        total_tag = root.find('.//nfe:ICMSTot/nfe:vNF', ns)
        if total_tag is not None:
            try:
                total_invoice_value = float(total_tag.text)
            except:
                total_invoice_value = 0.0

        # 3. Extrai os Produtos
        products = root.findall('.//nfe:det', ns)
        if not products:
            products = [elem for elem in root.iter() if elem.tag.endswith('det')]

        for det in products:
            prod = det.find('nfe:prod', ns) if det.find('nfe:prod', ns) is not None else det.find('prod')
            
            if prod is not None:
                name = prod.findtext('nfe:xProd', default=None, namespaces=ns) or prod.findtext('xProd')
                qty = prod.findtext('nfe:qCom', default=None, namespaces=ns) or prod.findtext('qCom')
                unit = prod.findtext('nfe:uCom', default=None, namespaces=ns) or prod.findtext('uCom')
                val = prod.findtext('nfe:vProd', default=None, namespaces=ns) or prod.findtext('vProd')
                code = prod.findtext('nfe:cEAN', default=None, namespaces=ns) or prod.findtext('cEAN')
                
                if not code or code == "SEM GTIN":
                    code = prod.findtext('nfe:cProd', default=None, namespaces=ns) or prod.findtext('cProd')

                if name and qty:
                    try:
                        v_prod = float(val) if val else 0.0
                        items_found.append({
                            "name": name.strip(),
                            "qty": float(qty),
                            "unit": unit.strip(),
                            "total_price": v_prod, # Importante manter para o cálculo de custo unitário
                            "code": code.strip() if code else ""
                        })
                    except:
                        pass

        # Fallback do total se não achou na tag vNF
        if total_invoice_value == 0 and items_found:
            total_invoice_value = sum(item['total_price'] for item in items_found)

        return {"items": items_found, "invoice_key": invoice_key, "total_value": total_invoice_value}

    except Exception as e:
        print(f"❌ Erro XML: {e}")
        return {"error": "XML inválido", "details": str(e)}

def normalize_items_with_ai(raw_items):
    """
    Usa o Gemini para limpar nomes técnicos e ajustar quantidades (Fardos -> Unidades).
    """
    print(f"🧠 [IA Normalizer] Otimizando {len(raw_items)} itens...")
    model = genai.GenerativeModel(MODEL_GEMINI)
    
    # Envia para a IA apenas o necessário para economizar e focar
    mini_list = [{"id": i, "txt": item['name'], "q": item['qty'], "u": item['unit']} for i, item in enumerate(raw_items)]
    
    prompt = f"""
    Você é um Especialista em Estoque de Restaurante. 
    Sua missão: LIMPAR a lista de produtos de uma Nota Fiscal XML e CONVERTER quantidades de atacado para varejo.
    
    DADOS ORIGINAIS: {json.dumps(mini_list, ensure_ascii=False)}

    REGRAS DE OURO:
    1. **Nomes Limpos:** Remova códigos, marcas irrelevantes e abreviações feias. 
       - "REF COCA COLA 2L PET" -> "Coca-Cola 2L"
       - "MUSS. PECA INTEIRA" -> "Queijo Mussarela"
       - "FAR. TRIGO ESP." -> "Farinha de Trigo"
    
    2. **Conversão de Fardos (Matemática):**
       - Se o nome ou unidade indicar caixa/fardo (ex: "CX", "FD", "C/12", "C/6"):
       - Multiplique a quantidade (q) pelo fator da caixa.
       - Mude a unidade (u) para "UN" (ou "KG/L" se apropriado).
       - Exemplo: "Coca Cola (FD C/6)" com q=2 -> Vira q=12 (2*6), u="UN".
       - Exemplo: "Tomate CX 20kg" com q=1 -> Vira q=20, u="KG".

    Retorne APENAS um JSON válido com os itens alterados:
    [
        {{"id": 0, "name": "Nome Limpo", "qty": 12.0, "unit": "UN"}},
        ...
    ]
    """
    
    try:
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        normalized = json.loads(response.text)
        
        if isinstance(normalized, dict): normalized = normalized.get('items', [])
            
        if isinstance(normalized, list):
            count_fixed = 0
            for n_item in normalized:
                idx = n_item.get('id')
                if idx is not None and idx < len(raw_items):
                    # Atualiza os dados originais
                    raw_items[idx]['name'] = n_item.get('name', raw_items[idx]['name'])
                    
                    try: raw_items[idx]['qty'] = float(n_item.get('qty'))
                    except: pass
                    
                    raw_items[idx]['unit'] = n_item.get('unit', raw_items[idx]['unit'])
                    count_fixed += 1
            print(f"✅ [IA Normalizer] {count_fixed} itens ajustados com sucesso.")
                    
        return raw_items
    except Exception as e:
        print(f"⚠️ IA Normalização falhou: {e}")
        return raw_items

def extract_data_from_invoice(file_bytes, mime_type="image/jpeg"):
    # 1. Fluxo XML
    if "xml" in mime_type or (file_bytes.startswith(b'<') and b'nfe' in file_bytes[:200].lower()):
        print("📄 [Leitor] Processando XML...")
        raw_result = parse_xml_nfe(file_bytes)
        
        # --- AQUI ESTAVA O PROBLEMA: Reativando a normalização ---
        if "items" in raw_result and raw_result["items"]:
            try:
                raw_result["items"] = normalize_items_with_ai(raw_result["items"])
            except Exception as e:
                print(f"Erro ao chamar IA no XML: {e}")
        # ---------------------------------------------------------
            
        return raw_result

    # 2. Fluxo Imagem/PDF (IA Direta)
    print(f"🤖 [Leitor] Processando {mime_type} com IA...")
    model = genai.GenerativeModel(MODEL_GEMINI)
    
    prompt = """
    Analise esta Nota Fiscal. Extraia:
    1. A CHAVE DE ACESSO (44 dígitos).
    2. O VALOR TOTAL da nota (vNF ou Total a Pagar).
    3. Os ITENS comprados.

    CRÍTICO - LIMPEZA E CONVERSÃO:
    - O campo "name" deve ser limpo e bonito (ex: "Coca-Cola 2L" ao invés de "REF. PET CC").
    - SE for Caixa/Fardo (ex: cx c/ 12, fd c/ 6), MULTIPLIQUE a quantidade para termos unidades unitárias e mude a unidade para UN.
    - Ex: Comprou 2 caixas de leite c/ 12. Retorne qty: 24, unit: "UN".

    Retorne JSON estrito:
    { 
        "invoice_key": "...", 
        "total_value": 0.00,
        "items": [
            {"name": "Nome Limpo", "qty": 0.0, "unit": "UN", "total_price": 0.0, "code": "EAN ou Cod"}
        ] 
    }
    """

    try:
        image_part = {"mime_type": mime_type, "data": file_bytes}
        response = model.generate_content([prompt, image_part], generation_config={"response_mime_type": "application/json"})
        
        # --- LIMPEZA DE SEGURANÇA ---
        # Remove formatação Markdown caso a IA envie ```json ... ```
        clean_text = response.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_text)
        # ----------------------------
        
        return data
        
    except Exception as e:
        # --- CORREÇÃO DE COTA (429) ---
        # Se der erro de cota, espera 5s e tenta o modelo estável (flash-latest)
        err_msg = str(e).lower()
        if "429" in err_msg or "quota" in err_msg or "resource exhausted" in err_msg:
            print(f"⚠️ [IA] Cota excedida. Aguardando 5s para tentar 'gemini-flash-latest'...")
            time.sleep(5) 
            
            try:
                # Recria o objeto de imagem para garantir
                img_retry = {"mime_type": mime_type, "data": file_bytes}
                
                # Tenta o modelo ESTÁVEL (Separado do 2.0)
                fallback_model = genai.GenerativeModel('gemini-flash-latest')
                
                response = fallback_model.generate_content(
                    [prompt, img_retry], 
                    generation_config={"response_mime_type": "application/json"}
                )
                
                clean_text = response.text.replace("```json", "").replace("```", "").strip()
                data = json.loads(clean_text)
                print("✅ [IA] Recuperado com sucesso via fallback!")
                return data
            except Exception as e2:
                print(f"❌ [IA] Falha no fallback: {e2}")
                return {"error": "Sistema Google sobrecarregado. Aguarde 30s e tente novamente.", "details": str(e2)}
        # ------------------------------

        print(f"❌ Erro Crítico IA: {str(e)}")
        return {"error": "Falha na leitura da nota.", "details": str(e)}