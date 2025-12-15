from sqlalchemy.orm import Session
from sqlalchemy import func, desc, and_, func, not_
from models import Order, Customer
from datetime import datetime, timedelta
import google.generativeai as genai
import os
import json
from dotenv import load_dotenv
from collections import Counter
from itertools import combinations


load_dotenv()

# Configura Google Gemini (Isso continua global por enquanto, ou pode vir do banco se cada cliente tiver sua chave)
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

class PizzaBrain:
    def __init__(self, db: Session, store_id: int):
        self.db = db
        self.store_id = store_id  # <--- O SEGREDO DO SAAS

    def _apply_filters(self, query, start_date=None, end_date=None, payment_method=None):
        """Aplica filtros de segurança (loja) + filtros de data/pagamento"""
        
        # 1. Filtro de Segurança (SaaS): Só traz pedidos desta loja
        query = query.filter(Order.store_id == self.store_id)
        
        # --- CORREÇÃO: Exclui Cancelados de todas as métricas ---
        query = query.filter(not_(Order.status.ilike("%CANCELADO%")))
        # --------------------------------------------------------
        
        # 2. Filtros de Interface
        if start_date:
            query = query.filter(Order.created_at >= start_date)
        
        if end_date:
            # Se for datetime, não alteramos. Se for só data, pegamos o final do dia.
            # A lógica principal de 23:59 já vem do main.py, aqui é apenas garantia.
            query = query.filter(Order.created_at <= end_date)
        
        if payment_method and payment_method != "Todos":
            query = query.filter(Order.payment_method.ilike(f"%{payment_method}%"))
            
        return query

    def get_kpis(self, start_date=None, end_date=None, payment_method=None):
        """Calcula faturamento, total de pedidos e ticket médio com filtros"""
        query = self.db.query(Order)
        
        # CORREÇÃO: Aplica os filtros antes de somar
        orders = self._apply_filters(query, start_date, end_date, payment_method).all()
        
        total_revenue = sum(o.total_value or 0 for o in orders)
        total_orders = len(orders)
        avg_ticket = total_revenue / total_orders if total_orders > 0 else 0
        
        estimated_profit = total_revenue * 0.30 

        return {
            "revenue": total_revenue,
            "orders": total_orders,
            "avg_ticket": avg_ticket,
            "estimated_profit": estimated_profit
        }

    def get_sales_heatmap(self, start_date=None, end_date=None):
        """Gera matriz para o mapa de calor baseada no período filtrado"""
        query = self.db.query(Order.created_at)
        orders = self._apply_filters(query, start_date, end_date).all()
        
        # Matriz 7 (dias) x 24 (horas)
        heatmap = [[0 for _ in range(24)] for _ in range(7)]
        
        # Fusos
        import pytz
        utc = pytz.utc
        br_zone = pytz.timezone('America/Sao_Paulo')
        
        for o in orders:
            if not o.created_at: continue
            
            # Pega data do banco (UTC) e converte para BR
            dt_utc = o.created_at.replace(tzinfo=utc) 
            dt_br = dt_utc.astimezone(br_zone)
            
            day_idx = dt_br.weekday() # 0 = Seg, 6 = Dom
            hour_idx = dt_br.hour
            heatmap[day_idx][hour_idx] += 1
            
        return heatmap

    def get_top_products(self, start_date=None, end_date=None, payment_method=None):
        """
        Retorna os produtos mais vendidos aplicando os mesmos filtros do Dashboard.
        """
        query = self.db.query(Order)
        
        # --- CORREÇÃO PRINCIPAL ---
        # Antes: Filtrava manualmente e esquecia do store_id e payment_method
        # Agora: Usa o _apply_filters para garantir consistência com os KPIs
        
        # Tratamento legado (caso venha um inteiro de dias ao invés de data)
        if isinstance(start_date, int):
             start_date = datetime.now() - timedelta(days=start_date)
        
        # Aplica filtros de Loja (SaaS), Data e Pagamento
        query = self._apply_filters(query, start_date, end_date, payment_method)
            
        orders = query.all()
        
        product_map = {}
        for o in orders:
            # Proteção contra pedidos sem itens
            if not o.items_json: continue
            
            # Garante que seja uma lista (caso o banco retorne algo estranho)
            items = o.items_json if isinstance(o.items_json, list) else []
            
            for item in items:
                # Tenta pegar o nome em várias chaves possíveis para robustez
                name = item.get('title') or item.get('item_name') or item.get('name') or "Produto s/ Nome"
                
                try:
                    qty = float(item.get('quantity', 1))
                    price = float(item.get('price', 0))
                except (ValueError, TypeError):
                    qty = 1
                    price = 0.0
                
                if name in product_map:
                    product_map[name]['qty'] += qty
                    product_map[name]['revenue'] += (qty * price)
                else:
                    product_map[name] = {'qty': qty, 'revenue': qty * price}
        
        # Ordena por Quantidade Vendida (Decrescente)
        sorted_products = sorted(product_map.items(), key=lambda x: x[1]['qty'], reverse=True)
        
        # Retorna Top 10 formatado para o Gráfico
        return [{"name": k, "qty": v['qty'], "revenue": v['revenue']} for k, v in sorted_products][:10]
    

    def get_customer_intelligence(self, days_lookback=90):
        # CRM olha um período fixo para trás
        start_date = datetime.now() - timedelta(days=days_lookback)
        
        # Query filtrada apenas pela loja e data fixa (sem filtro de tela)
        orders = self.db.query(Order).filter(
            Order.store_id == self.store_id,
            Order.created_at >= start_date
        ).all()
        
        customers = {}
        for o in orders:
            key = o.customer_phone
            if not key: continue
            
            if key not in customers:
                customers[key] = {"name": o.customer_name, "count": 0, "total_spent": 0, "last_order": o.created_at}
            
            c = customers[key]
            c["count"] += 1
            c["total_spent"] += float(o.total_value or 0)
            if o.created_at > c["last_order"]: c["last_order"] = o.created_at

        customer_list = []
        now = datetime.now()
        
        for phone, data in customers.items():
            days_since = (now - data["last_order"]).days
            status = "Ativo"
            
            # --- CORREÇÃO: "Risco" agora é > 15 dias (Ciclo Rápido) ---
            if days_since > 15: status = "Risco"
            
            customer_list.append({
                "name": data["name"], "phone": phone, "orders": data["count"],
                "spent": data["total_spent"], "last_order_days": days_since, "status": status
            })
            
        vips = sorted(customer_list, key=lambda x: x["spent"], reverse=True)[:10]
        # Considera risco se estiver em "Risco" (>15 dias) e tiver pelo menos 2 pedidos (já foi cliente)
        churn_risk = sorted([c for c in customer_list if c["status"] == "Risco" and c["orders"] >= 2], key=lambda x: x["spent"], reverse=True)[:10]
        
        return {"vips": vips, "churn_risk": churn_risk, "total_active": len(customer_list)}

    def generate_creative_scripts(self, start_date, end_date):
        """Gera roteiros de vídeos baseados em dados reais da loja"""
        kpis = self.get_kpis(start_date, end_date)
        top_prods = self.get_top_products(start_date, end_date)
        heatmap = self.get_sales_heatmap(start_date, end_date)
        
        # Descobre o "Dia Fraco"
        days_sum = [sum(day_row) for day_row in heatmap]
        weekdays = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
        
        active_days = [(i, val) for i, val in enumerate(days_sum) if val > 0]
        if active_days:
            weakest_day_idx = min(active_days, key=lambda x: x[1])[0]
            weakest_day_name = weekdays[weakest_day_idx]
        else:
            weakest_day_name = "dias de semana"

        top_pizza = top_prods[0]['name'] if top_prods else "Pizza Especial"

        prompt = f"""
        Aja como um Estrategista de Conteúdo para Delivery (TikTok/Reels).
        Dados da Loja:
        - Produto Estrela: {top_pizza}
        - Dia Fraco: {weakest_day_name}
        - Ticket Médio: R$ {kpis['avg_ticket']:.2f}

        Crie 3 Roteiros de Anúncios Curtos (15s):
        1. 🎥 **Foco no Produto**: Food Porn do {top_pizza}.
        2. 📉 **Ação para Dia Fraco**: Ideia para bombar a {weakest_day_name}.
        3. 🤳 **Bastidores**: Algo simples da operação.

        Formato: Cena Visual | Texto Falado | Call to Action.
        """

        try:
            model = genai.GenerativeModel('gemini-2.0-flash') 
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            return f"❌ Erro ao gerar criativos: {str(e)}"

    def generate_virtual_manager_briefing(self, kpis, top_prods, cust_intel):
        summary = {
            "faturamento": kpis['revenue'],
            "ticket_medio": kpis['avg_ticket'],
            "top_produtos": [p['name'] for p in top_prods[:5]],
            "clientes_risco_churn": len(cust_intel['churn_risk'])
        }

        return f"""
        ATUE COMO UM GERENTE DE RESTAURANTE SÊNIOR.
        Analise os dados desta loja:
        {json.dumps(summary, indent=2, ensure_ascii=False)}

        Crie um briefing curto e direto:
        1. 📢 Insight Principal.
        2. 💰 Sugestão prática para subir o Ticket Médio (Atual: R$ {summary['ticket_medio']:.2f}).
        3. 📉 Ação de recuperação para os {summary['clientes_risco_churn']} clientes sumidos.
        """

    def ask_gemini_strategist(self, start_date, end_date, payment_method):
        kpis = self.get_kpis(start_date, end_date, payment_method)
        top = self.get_top_products(start_date, end_date, payment_method)
        cust = self.get_customer_intelligence(90)

        prompt = self.generate_virtual_manager_briefing(kpis, top, cust)

        try:
            model = genai.GenerativeModel('gemini-2.0-flash') 
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            return f"❌ Erro IA: {str(e)}"
        
    def generate_daily_report_text(self):
        """Gera o relatório detalhado de ontem para o WhatsApp"""
        
        # 1. Define o intervalo "Ontem"
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        start_dt = datetime.combine(yesterday, datetime.min.time())
        end_dt = datetime.combine(yesterday, datetime.max.time())
        
        # 2. Coleta Dados
        kpis = self.get_kpis(start_dt, end_dt)
        
        # Se não vendeu nada, aborta
        if kpis['orders'] == 0:
            return None

        # Pega os Top 15 produtos (para não ficar uma mensagem infinita)
        # Precisamos garantir que get_top_products aceite datas
        top_products_list = self.get_top_products(start_dt, end_dt)
        
        # Formata a lista para texto simples antes de enviar pra IA
        products_text = ""
        for p in top_products_list:
            products_text += f"- {p['qty']}x {p['name']} (R$ {p['revenue']:.2f})\n"

        # 3. Monta o Resumo de Dados
        data_summary = {
            "data": yesterday.strftime("%d/%m/%Y"),
            "faturamento": f"R$ {kpis['revenue']:.2f}",
            "pedidos": kpis['orders'],
            "ticket_medio": f"R$ {kpis['avg_ticket']:.2f}",
            "lista_produtos": products_text
        }

        # 4. Prompt Rico para a IA
        prompt = f"""
        Aja como um Gerente de Pizzaria. Monte um relatório de fechamento de caixa para ser enviado no WhatsApp do dono.
        Use estes dados reais de ONTEM:
        {json.dumps(data_summary, indent=2, ensure_ascii=False)}

        Regras de Formatação (WhatsApp):
        - Use *Negrito* para valores monetários e totais.
        - Use muitos emojis relevantes (💰, 🍕, 🥤, 🚀).
        - Crie uma separação visual bonita.

        Estrutura Obrigatória:
        1. Cabeçalho com Data e Faturamento Total (Em destaque).
        2. Métricas: Pedidos e Ticket Médio.
        3. 📋 **Resumo do Cardápio** (Liste os itens vendidos no formato: "Qtd x Nome").
        4. 💡 **Análise Rápida:** Uma frase curta sobre o desempenho (ex: "Ticket médio subiu!" ou "A Esfiha liderou hoje").

        Não invente dados, use apenas o JSON fornecido.
        """

        try:
            model = genai.GenerativeModel('gemini-2.0-flash') 
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            # Fallback seguro se a IA falhar
            return (
                f"📅 *Resumo {data_summary['data']}*\n\n"
                f"💰 Faturamento: *{data_summary['faturamento']}*\n"
                f"📦 Pedidos: {data_summary['pedidos']} (TM: {data_summary['ticket_medio']})\n\n"
                f"📋 *Vendidos:*\n{products_text}"
            )
            
        
    def analyze_combos(self, min_support=5):
        """
        Descobre produtos que são comprados juntos frequentemente.
        min_support: Mínimo de vezes que o par deve aparecer para ser relevante.
        """
        # 1. Pega os últimos 500 pedidos (para ser rápido)
        orders = self.db.query(Order).filter(
            Order.store_id == self.store_id
        ).order_by(desc(Order.created_at)).limit(500).all()
        
        pair_counts = Counter()
        
        for order in orders:
            if not order.items_json: continue
            
            # Extrai nomes dos produtos únicos neste pedido
            items = set()
            for item in order.items_json:
                name = item.get('title', '').split('(')[0].strip() # Limpa nome
                if name: items.add(name)
            
            # Se tiver 2 ou mais itens, gera pares
            if len(items) >= 2:
                # Ordena para que (Coca, Esfiha) seja igual a (Esfiha, Coca)
                pairs = list(combinations(sorted(items), 2))
                pair_counts.update(pairs)
        
        # 2. Pega o par mais frequente
        if not pair_counts: return None
        
        best_pair, count = pair_counts.most_common(1)[0]
        
        if count >= min_support:
            item_a, item_b = best_pair
            
            # Gera o Insight
            return {
                "title": f"Combo Ouro: {item_a} + {item_b}",
                "message": f"Detectamos que **{count} clientes** compraram '{item_a}' e '{item_b}' juntos recentemente. Eles são sua dupla dinâmica!",
                "prompt": f"Crie uma imagem publicitária profissional e apetitosa para um delivery de comida. A imagem deve destacar um combo promocional contendo: {item_a} e {item_b}. Iluminação de estúdio, estilo food porn, fundo escuro elegante, fumaça saindo da comida quente, 4k, ultra realista."
            }
        
        return None
    
    def generate_campaign_blueprint(self, goal: str):
        """
        Gera uma estratégia completa de campanha: Texto, Nome do Template e Variáveis.
        """
        prompt = f"""
        Aja como um Especialista em Marketing de WhatsApp (Meta Business).
        Eu quero criar uma campanha com o seguinte objetivo: "{goal}".
        
        Gere um Template Oficial otimizado para conversão.
        Retorne APENAS um JSON com este formato exato:
        {{
            "template_name": "nome_tecnico_em_minusculo_v1",
            "message_body": "O texto da mensagem com emojis e variáveis assim {{1}} {{2}}...",
            "variables_explanation": "Explique o que colocar em cada variável (ex: {{1}} é o nome)",
            "category": "MARKETING"
        }}
        Seja criativo, persuasivo e use gatilhos mentais.
        """

        try:
            model = genai.GenerativeModel('gemini-2.0-flash') 
            response = model.generate_content(prompt)
            # Limpa blocos de código se a IA mandar ```json ... ```
            clean_text = response.text.replace("```json", "").replace("```", "").strip()
            return json.loads(clean_text)
        except Exception as e:
            return {"error": str(e)}
        
    def run_rfm_segmentation(self):
        """
        Analisa clientes e gera Insights (Versão com Prioridade Corrigida).
        """
        from models import Insight, Customer
        
        customers = self.db.query(Customer).filter(
            Customer.store_id == self.store_id,
            Customer.last_order_at.isnot(None),
            Customer.total_spent > 0
        ).all()
        
        if not customers: return 0
        
        print(f"🧠 [RFM] Analisando {len(customers)} clientes (Ciclo Rápido)...")
        alerts_generated = 0

        for cust in customers:
            old_status = cust.rfm_segment
            
            # --- LÓGICA DE PONTUAÇÃO ---
            days_since = (datetime.now() - cust.last_order_at).days
            money = cust.total_spent or 0
            
            is_test_customer = "Teste Churn" in (cust.name or "")
            
            # Régua de Recência (R)
            if days_since <= 7: r_score = 5
            elif days_since <= 14: r_score = 4
            elif days_since <= 21: r_score = 3
            elif days_since <= 30: r_score = 2
            else: r_score = 1
            
            # Régua de Valor (M)
            if money >= 600: m_score = 5
            elif money >= 300: m_score = 4
            elif money >= 150: m_score = 3
            elif money >= 80: m_score = 2
            else: m_score = 1
            
            # --- SEGMENTAÇÃO (PRIORIDADE CORRIGIDA) ---
            segment = "Comum"
            
            # 1. Topo da Pirâmide (Campeão)
            if r_score >= 4 and m_score >= 4: 
                segment = "💎 Campeão"
            
            # 2. ALERTA VERMELHO (Subimos a prioridade deste!)
            # Se ele gasta muito (M>=4) e a frequência caiu (R<=3), é Risco Imediato
            elif r_score <= 3 and m_score >= 4: 
                segment = "⚠️ Em Risco (VIP)" 
            
            # 3. Base Leal
            elif r_score >= 4 and m_score >= 2: 
                segment = "🚀 Leal"
            
            # 4. Potenciais
            elif r_score >= 3 and m_score >= 3: 
                segment = "✅ Promissor"
            
            # 5. Perdidos / Baixo Valor
            elif r_score <= 2 and m_score <= 2: 
                segment = "💤 Hibernando"
            
            # 6. Novos
            elif r_score >= 4 and m_score == 1: 
                segment = "👶 Novato"
            
            if is_test_customer:
                print(f"   🔎 DETETIVE: {cust.name} | Dias: {days_since} (R={r_score}) | Valor: {money} (M={m_score}) | Status: {segment}")

            # Salva
            cust.rfm_segment = segment
            cust.rfm_score = f"{r_score}{m_score}"
            
            # --- GERA O INSIGHT AUTOMÁTICO ---
            if segment == "⚠️ Em Risco (VIP)" and old_status != "⚠️ Em Risco (VIP)":
                
                last_alert = self.db.query(Insight).filter(
                    Insight.store_id == self.store_id,
                    Insight.title.ilike(f"%{cust.name}%"),
                    Insight.created_at >= datetime.now() - timedelta(days=15)
                ).first()
                
                if not last_alert:
                    insight = Insight(
                        store_id=self.store_id,
                        type="churn_alert",
                        title=f"🚨 Alerta: {cust.name} sumiu!",
                        message=f"O cliente VIP **{cust.name}** (Total: R$ {money:.2f}) não pede há {days_since} dias.",
                        action_prompt=f"Crie uma mensagem curta para {cust.name} oferecendo um cupom VIP.",
                        is_read=False,
                        is_archived=False
                    )
                    self.db.add(insight)
                    alerts_generated += 1
                    if is_test_customer: print(f"   ✅ ALERTA CRIADO PARA {cust.name}!")
                else:
                    if is_test_customer: print(f"   ⛔ ALERTA BLOQUEADO (Duplicidade): Já existe um recente.")

        self.db.commit()
        return len(customers)
    
    def generate_inventory_forecast(self, analysis_start, analysis_end, days_to_cover):
        from models import Product, Order
        import math
        
        print(f"🔮 [IA Estoque] Analisando de {analysis_start} até {analysis_end} para cobrir {days_to_cover} dias.")
        
        # 1. Busca Pedidos
        orders = self.db.query(Order).filter(
            Order.store_id == self.store_id,
            Order.created_at >= analysis_start,
            Order.created_at <= analysis_end
        ).all()
        
        if not orders:
            return {"shopping_list": [], "message": "Nenhuma venda encontrada."}

        # 2. Calcula Vendas
        product_sales_total = {}
        for order in orders:
            if not order.items_json: continue
            for item in order.items_json:
                name = item.get('title', '').split('(')[0].strip()
                qty = float(item.get('quantity', 1))
                product_sales_total[name] = product_sales_total.get(name, 0) + qty
        
        # 3. Média Diária
        delta_days = (analysis_end - analysis_start).days
        if delta_days < 1: delta_days = 1
        daily_avg = {k: v / delta_days for k, v in product_sales_total.items()}
        
        # 4. Projeção
        predicted_need = {k: v * days_to_cover for k, v in daily_avg.items()}
        
        # 5. Explosão (Ingredientes)
        ingredients_needed = {}
        
        for prod_name, qty_needed in predicted_need.items():
            product_db = self.db.query(Product).filter(
                Product.store_id == self.store_id, 
                Product.name.ilike(f"%{prod_name}%")
            ).first()
            
            if product_db and product_db.recipe_items:
                for item in product_db.recipe_items:
                    if not item.ingredient: continue
                    ing = item.ingredient
                    
                    factor = ing.conversion_factor or 1.0
                    ing_qty = (item.quantity * qty_needed) / factor
                    
                    if ing.name not in ingredients_needed:
                        ingredients_needed[ing.name] = {
                            "qty": 0.0, 
                            "unit": ing.unit_rel.name if ing.unit_rel else "UN",
                            "cost": ing.cost or 0.0
                        }
                    
                    ingredients_needed[ing.name]["qty"] += ing_qty

        # Formata para Tabela (Arredondando para cima)
        shopping_list = []
        total_estimated = 0.0
        
        for name, data in ingredients_needed.items():
            if data['qty'] > 0.01:
                qty_buy = math.ceil(data['qty']) # Arredonda para cima (1.2 -> 2)
                cost_total = qty_buy * data['cost']
                
                shopping_list.append({
                    "name": name,
                    "qty_exact": data['qty'],
                    "qty_buy": qty_buy, # Inteiro para compra
                    "unit": data['unit'],
                    "unit_price": data['cost'],
                    "total_cost": cost_total
                })
                total_estimated += cost_total
        
        predicted_display = {k: round(v, 1) for k, v in predicted_need.items() if v > 0.5}

        return {
            "predicted_products": predicted_display,
            "shopping_list": sorted(shopping_list, key=lambda x: x['name']),
            "total_estimated": total_estimated
        }
        
        
    def calculate_inventory_usage(self, order_items):
        """
        Recebe os itens PADRONIZADOS (do Adapter) e calcula a baixa de estoque
        considerando bases, tamanhos e meio-a-meia.
        """
        from models import Product, ProductMapping, PizzaBaseRecipe
        
        usage_report = {} # { "Mussarela": 0.5, "Caixa G": 1.0 }

        for item in order_items:
            # item vem do JSON padronizado: {'title': '...', 'quantity': 1, 'parts': [...]}
            
            # 1. Tenta identificar o Produto Principal (pelo mapeamento ou nome)
            # (Num cenário ideal, o Adapter já teria buscado o product_id via ProductMapping)
            # Aqui vamos simular a lógica inteligente:
            
            # Detecta tamanho pelo nome (Ex: "Pizza Calabresa G") ou metadados
            size_slug = "familia" # Default seguro
            if "broto" in item['title'].lower(): size_slug = "broto"
            elif "édia" in item['title'].lower() or "edia" in item['title'].lower(): size_slug = "media"
            
            qty_sold = float(item['quantity'])

            # --- A. BAIXA DA BASE (Massa, Caixa, etc) ---
            # Busca a receita padrão para esse tamanho nesta loja
            base_recipes = self.db.query(PizzaBaseRecipe).filter(
                PizzaBaseRecipe.store_id == self.store_id,
                PizzaBaseRecipe.size_slug == size_slug
            ).all()
            
            for base in base_recipes:
                ing_name = base.ingredient.name
                total_qty = base.quantity * qty_sold
                usage_report[ing_name] = usage_report.get(ing_name, 0) + total_qty

            # --- B. BAIXA DO RECHEIO (Meio-a-Meia Inteligente) ---
            
            # Verifica se tem partes (meio a meio) vindo do Adapter
            parts = item.get('parts', []) # O Adapter Wabiz tem que preencher isso!
            
            if parts:
                # É Meio-a-Meia!
                fraction = 1.0 / len(parts) # Se for 2 sabores, 0.5. Se 3, 0.33.
                
                for part_name in parts:
                    # Busca o produto "Sabor" no banco
                    product_sabor = self.db.query(Product).filter(
                        Product.store_id == self.store_id,
                        Product.name.ilike(f"%{part_name}%") # Busca aproximada
                    ).first()
                    
                    if product_sabor:
                        for recipe in product_sabor.recipe_items:
                            ing_name = recipe.ingredient.name
                            # Qtd Receita * Qtd Pedida * Fração (0.5)
                            total_ing = recipe.quantity * qty_sold * fraction
                            usage_report[ing_name] = usage_report.get(ing_name, 0) + total_ing
            
            else:
                # É Pizza Inteira de um sabor só
                # Busca produto pelo nome do item
                product_full = self.db.query(Product).filter(
                    Product.store_id == self.store_id,
                    Product.name.ilike(f"%{item['title']}%")
                ).first()
                
                if product_full:
                    for recipe in product_full.recipe_items:
                        ing_name = recipe.ingredient.name
                        usage_report[ing_name] = usage_report.get(ing_name, 0) + (recipe.quantity * qty_sold)

        return usage_report
    
    
    # Em pizzaria/services/analytics.py (Dentro da classe PizzaBrain)

    def get_stock_consumption_report(self, start_date, end_date):
        """
        Calcula o consumo teórico de ingredientes no período.
        Cruza as vendas com as fichas técnicas.
        """
        from models import Ingredient, Order # Import local para evitar ciclo
        
        # 1. Busca Pedidos no período
        orders = self.db.query(Order).filter(
            Order.store_id == self.store_id,
            Order.created_at >= start_date,
            Order.created_at <= end_date
        ).all()
        
        # 2. Cache de Ingredientes (para pegar nome, custo e CATEGORIA)
        all_ings = self.db.query(Ingredient).filter(Ingredient.store_id == self.store_id).all()
        # Mapeia pelo nome para facilitar o cruzamento com o calculate_inventory_usage
        ing_db_map = {ing.name: ing for ing in all_ings}
        
        consumption_data = {} 
        
        for order in orders:
            if not order.items_json: continue
            
            # Explode itens do pedido em ingredientes (usa a função que já existe na classe)
            usage = self.calculate_inventory_usage(order.items_json)
            
            for ing_name, qty_used in usage.items():
                # Tenta achar o ingrediente no banco pelo nome
                ing_obj = ing_db_map.get(ing_name)
                
                if not ing_obj: continue
                
                # --- CORREÇÃO DA CATEGORIA ---
                cat_name = "Geral"
                if ing_obj.category_rel:
                    cat_name = ing_obj.category_rel.name
                elif ing_obj.category_legacy:
                    cat_name = ing_obj.category_legacy
                # -----------------------------
                
                # Conversão para unidade de estoque (Baixa / Fator)
                factor = ing_obj.conversion_factor if (ing_obj.conversion_factor and ing_obj.conversion_factor > 0) else 1.0
                qty_stock = qty_used / factor
                
                # Custo estimado
                cost = qty_stock * ing_obj.cost
                
                if ing_name not in consumption_data:
                    consumption_data[ing_name] = {
                        "name": ing_name,
                        "qty": 0.0,
                        "unit": ing_obj.unit_rel.name if ing_obj.unit_rel else "UN",
                        "cost": 0.0,
                        "category": cat_name # <--- Agora preenchido corretamente
                    }
                
                consumption_data[ing_name]["qty"] += qty_stock
                consumption_data[ing_name]["cost"] += cost

        # Retorna lista ordenada por custo (Curva ABC de valor)
        return sorted(consumption_data.values(), key=lambda x: x['cost'], reverse=True)