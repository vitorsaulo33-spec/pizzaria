# Arquivo: pizzaria/services/stock_engine.py

from sqlalchemy import or_, func
from sqlalchemy.orm import Session
# ADICIONADO: InventoryCategory no import
from models import (
    Product, ProductMapping, Ingredient, PizzaBaseRecipe, ProductRecipe, 
    Category, PizzaSize, ProductAddon, AddonPrice, InventoryUnit, 
    InventoryCategory, StockLog
)

# ==========================================
#          RESOLUÇÃO DE PRODUTOS
# ==========================================

def _resolve_product(db, store_id, item_data, integration_source):
    """
    Localiza o produto no banco de dados.
    ESTRATÉGIA BLINDADA:
    1. ID Interno (Prioridade Absoluta - Vem do PDV).
    2. Código Externo (Integrações).
    3. Nome (Último caso).
    """
    ext_code = ""
    name = ""
    prod_id = None 
    
    if isinstance(item_data, dict):
        raw_code = str(item_data.get('external_code') or '').strip()
        ext_code = raw_code if raw_code.lower() != 'none' else ""
        
        # Tenta pegar o ID interno seguro
        raw_id = str(item_data.get('product_id') or '').strip()
        if raw_id.isdigit() and int(raw_id) > 0:
            prod_id = int(raw_id)
        
        name = item_data.get('name', '').split('(')[0].strip()
        
    elif isinstance(item_data, str):
        name = item_data.strip()

    # 1. VIA EXPRESSA: ID INTERNO
    if prod_id:
        product = db.query(Product).filter(Product.id == prod_id, Product.store_id == store_id).first()
        if product:
            return product

    # 2. BUSCA POR CÓDIGO EXTERNO
    if ext_code:
        mapping = db.query(ProductMapping).filter(
            ProductMapping.store_id == store_id,
            ProductMapping.external_code == ext_code
        ).first()
        if mapping: return mapping.product
        return None

    # 3. BUSCA POR NOME
    if name:
        return db.query(Product).filter(
            Product.store_id == store_id, 
            Product.name.ilike(name)
        ).first()
        
    return None


def _detect_size_from_text(db: Session, store_id: int, text_to_search: str):
    if not text_to_search: return None
    all_sizes = db.query(PizzaSize).filter(PizzaSize.store_id == store_id).all()
    text_lower = text_to_search.lower()
    best_match = None
    for size in all_sizes:
        if size.name.lower() in text_lower:
            if best_match is None or len(size.name) > len(best_match.name):
                best_match = size
    return best_match


# ==========================================
#          AUTO-APRENDIZAGEM
# ==========================================

def auto_learn_product(db: Session, store_id: int, item_data: dict, integration_source: str):
    external_code = str(item_data.get("external_code", "")).strip()
    if external_code.lower() in ['none', 'null', '']: external_code = ""
    
    raw_name = item_data.get("title", item_data.get("name", "")).strip()
    source_clean = integration_source.lower().strip()

    if not external_code and not raw_name: return None

    READ_ONLY_SOURCES = ['manual', 'teste', 'pdv', 'balcao', 'mesa']

    if item_data.get('product_id'):
        return None

    # 1. Busca Global
    if external_code:
        existing_mapping = db.query(ProductMapping).filter(
            ProductMapping.store_id == store_id,
            ProductMapping.external_code == external_code
        ).first()

        if existing_mapping:
            product_found = existing_mapping.product
            if source_clean in READ_ONLY_SOURCES: return product_found

            if existing_mapping.integration_type != source_clean:
                specific_exists = db.query(ProductMapping).filter(
                    ProductMapping.store_id == store_id,
                    ProductMapping.integration_type == source_clean,
                    ProductMapping.external_code == external_code
                ).first()
                if not specific_exists:
                    try:
                        new_map = ProductMapping(
                            store_id=store_id, product_id=product_found.id,
                            integration_type=source_clean, external_code=external_code
                        )
                        db.add(new_map); db.commit()
                    except: db.rollback()
            return product_found

    # 2. Busca Nome
    clean_name = raw_name.split("(")[0].strip()
    product = db.query(Product).filter(Product.store_id == store_id, Product.name.ilike(clean_name)).first()

    if product:
        if source_clean in READ_ONLY_SOURCES: return product
        if external_code:
            try:
                new_map = ProductMapping(
                    store_id=store_id, product_id=product.id,
                    integration_type=source_clean, external_code=external_code
                )
                db.add(new_map); db.commit()
            except: db.rollback()
        return product
    
    return None


# ==========================================
#          BAIXA DE ESTOQUE
# ==========================================

def deduct_stock_from_order(db: Session, store_id: int, items: list, integration_source: str = "manual"):
    print(f"📉 [Estoque] Processando {len(items)} itens via {integration_source}...")

    for item in items:
        qty_sold = float(item.get("quantity", 1))
        parts = item.get("parts", [])   
        addons = item.get("addons", []) 
        
        product = _resolve_product(db, store_id, {
            "name": item.get("title", ""),
            "external_code": item.get("external_code"),
            "product_id": item.get("product_id")
        }, integration_source)

        removed_ids = item.get("removed_ingredients", [])

        # CASO A: PIZZA
        if product and product.is_pizza:
            item_title = item.get("title", "")
            size_obj = _detect_size_from_text(db, store_id, item_title)

            if not size_obj and parts:
                for p_data in parts:
                    p_name = p_data.get('name') if isinstance(p_data, dict) else p_data
                    size_obj = _detect_size_from_text(db, store_id, p_name)
                    if size_obj: break
            
            if not size_obj:
                size_obj = db.query(PizzaSize).filter(PizzaSize.store_id == store_id).order_by(PizzaSize.slices.desc()).first()

            # Base
            if size_obj:
                base_query = db.query(PizzaBaseRecipe).filter(
                    PizzaBaseRecipe.store_id == store_id,
                    PizzaBaseRecipe.base_type == product.base_type,
                    or_(PizzaBaseRecipe.size_id == size_obj.id, PizzaBaseRecipe.size_slug == size_obj.slug)
                )
                for base in base_query.all():
                    if base.ingredient:
                        _execute_stock_movement(db, store_id, base.ingredient, base.quantity * qty_sold, "OUT", f"Venda {item_title[:15]}")

            # Sabores
            if parts:
                fraction = 1.0 / len(parts)
                for part_data in parts:
                    _process_recursive_item(db, store_id, part_data, qty_sold * fraction, integration_source, size_obj, removed_ids, is_pizza_part=True)
            else:
                _process_recursive_item(db, store_id, {
                    "external_code": item.get("external_code"), 
                    "product_id": item.get("product_id"),
                    "name": item.get("title")
                }, qty_sold, integration_source, size_obj, removed_ids, is_pizza_part=True)

            # Bordas/Extras
            for addon_data in addons:
                _process_recursive_item(db, store_id, addon_data, qty_sold, integration_source, size_obj, removed_ids, is_addon=True)

        # CASO B: PRODUTO COMUM
        elif product:
            _deduct_recipe(db, store_id, product, qty_sold, removed_ids)
            all_subs = parts + addons
            for sub in all_subs:
                _process_recursive_item(db, store_id, sub, qty_sold, integration_source, None, [])

    db.commit()


def _process_recursive_item(db, store_id, item_data, parent_qty, source, size_obj=None, removed_ids=[], is_pizza_part=False, is_addon=False):
    if isinstance(item_data, str): item_data = {"name": item_data}

    product = _resolve_product(db, store_id, item_data, source)
    
    addon_obj = None
    if is_addon and not product:
        ext_code = str(item_data.get('external_code') or '').strip()
        if ext_code and ext_code != 'None':
            ap = db.query(AddonPrice).filter(AddonPrice.external_code == ext_code).first()
            if ap: addon_obj = ap.addon

    qty_item = float(item_data.get('quantity', 1)) * parent_qty

    if product:
        if is_pizza_part:
            recipes = []
            if size_obj:
                recipes = db.query(ProductRecipe).filter(ProductRecipe.product_id == product.id, ProductRecipe.size_id == size_obj.id).all()
            if not recipes:
                recipes = db.query(ProductRecipe).filter(ProductRecipe.product_id == product.id, ProductRecipe.size_id == None).all()
            
            for r in recipes:
                if r.ingredient and r.ingredient.id not in removed_ids:
                    size_factor = size_obj.recipe_multiplier if (size_obj and not r.size_id) else 1.0
                    _execute_stock_movement(db, store_id, r.ingredient, r.quantity * qty_item * size_factor, "OUT", f"Venda {product.name}")
        else:
            _deduct_recipe(db, store_id, product, qty_item, removed_ids)

    elif addon_obj and size_obj:
        addon_price = db.query(AddonPrice).filter(AddonPrice.addon_id == addon_obj.id, AddonPrice.size_id == size_obj.id).first()
        if addon_price:
            for r in addon_price.recipe_items:
                if r.ingredient:
                    _execute_stock_movement(db, store_id, r.ingredient, r.quantity * qty_item, "OUT", f"Venda {addon_obj.name}")

    sub_items = item_data.get('sub_items', [])
    if sub_items:
        for sub in sub_items:
            _process_recursive_item(db, store_id, sub, qty_item, source, None, [])


def _deduct_recipe(db, store_id, product, qty, removed_ids):
    for r in product.recipe_items:
        if r.ingredient and r.ingredient.id not in removed_ids:
            _execute_stock_movement(db, store_id, r.ingredient, r.quantity * qty, "OUT", f"Venda {product.name}")


def _execute_stock_movement(db, store_id, ingredient, quantity_needed, type, reason):
    factor = ingredient.conversion_factor if (ingredient.conversion_factor and ingredient.conversion_factor > 0) else 1.0
    real_deduct = quantity_needed / factor
    old_stock = ingredient.current_stock
    
    if type == "OUT":
        ingredient.current_stock -= real_deduct
    else:
        ingredient.current_stock += real_deduct
        
    log = StockLog(
        store_id=store_id, ingredient_id=ingredient.id, movement_type=type,
        quantity=real_deduct, old_stock=old_stock, new_stock=ingredient.current_stock,
        cost_at_time=ingredient.cost, reason=reason, user_name="Sistema Auto"
    )
    db.add(log)

    
def return_stock_from_order(db: Session, store_id: int, items: list, integration_source: str = "manual"):
    print(f"🔄 [Estoque] Estornando {len(items)} itens (Fonte: {integration_source})...")

    for item in items:
        qty_sold = float(item.get("quantity", 1))
        parts = item.get("parts", [])
        addons = item.get("addons", [])
        
        product = _resolve_product(db, store_id, {
            "name": item.get("title", "").split("(")[0].strip(),
            "external_code": item.get("external_code"),
            "product_id": item.get("product_id")
        }, integration_source)

        if not product: continue

        removed_ids = item.get("removed_ingredients", [])

        if product.is_pizza:
            item_title = item.get("title", "")
            size_obj = _detect_size_from_text(db, store_id, item_title)

            if not size_obj and parts:
                for p_data in parts:
                    p_name = p_data.get('name') if isinstance(p_data, dict) else p_data
                    size_obj = _detect_size_from_text(db, store_id, p_name)
                    if size_obj: break

            if not size_obj:
                size_obj = db.query(PizzaSize).filter(PizzaSize.store_id == store_id).order_by(PizzaSize.slices.desc()).first()

            # 1. Estorno da Base
            base_query = db.query(PizzaBaseRecipe).filter(
                PizzaBaseRecipe.store_id == store_id,
                PizzaBaseRecipe.base_type == product.base_type
            )
            if size_obj:
                base_query = base_query.filter(or_(
                    PizzaBaseRecipe.size_id == size_obj.id,
                    PizzaBaseRecipe.size_slug == size_obj.slug
                ))

            for base in base_query.all():
                if base.ingredient:
                    _execute_stock_movement(db, store_id, base.ingredient, base.quantity * qty_sold, "IN", f"Estorno {item_title[:20]}")

            # 2. Estorno dos Sabores
            flavors_to_return = []
            if parts:
                fraction = 1.0 / len(parts)
                for part_data in parts:
                    if isinstance(part_data, str): part_data = {"name": part_data}
                    p_part = _resolve_product(db, store_id, part_data, integration_source)
                    if p_part:
                        flavors_to_return.append((p_part, fraction))
            else:
                flavors_to_return.append((product, 1.0))

            for p_obj, mult in flavors_to_return:
                recipes = []
                if size_obj:
                    recipes = db.query(ProductRecipe).filter(
                        ProductRecipe.product_id == p_obj.id,
                        ProductRecipe.size_id == size_obj.id
                    ).all()
                if not recipes:
                    recipes = db.query(ProductRecipe).filter(
                        ProductRecipe.product_id == p_obj.id,
                        ProductRecipe.size_id == None
                    ).all()

                for r in recipes:
                    if r.ingredient and r.ingredient.id not in removed_ids:
                        size_factor = size_obj.recipe_multiplier if (size_obj and not r.size_id) else 1.0
                        raw_qty = r.quantity * qty_sold * mult * size_factor
                        _execute_stock_movement(db, store_id, r.ingredient, raw_qty, "IN", f"Estorno Pizza {p_obj.name}")

            # 3. Estorno de Bordas/Extras
            # (Mantido como estava, pois usa a lógica interna do _execute)

        else:
            for recipe in product.recipe_items:
                if recipe.ingredient and recipe.ingredient.id not in removed_ids:
                    _execute_stock_movement(db, store_id, recipe.ingredient, recipe.quantity * qty_sold, "IN", f"Estorno {product.name}")

    db.commit()


# ==========================================
#           GESTÃO DE ESTOQUE (RESTORED)
# ==========================================

def get_or_create_category(db, store_id, name):
    """
    Função auxiliar usada pelo router inventory.py
    """
    if not name:
        return None
    cat = (
        db.query(InventoryCategory)
        .filter(
            InventoryCategory.store_id == store_id, InventoryCategory.name.ilike(name)
        )
        .first()
    )
    if not cat:
        cat = InventoryCategory(store_id=store_id, name=name.title())
        db.add(cat)
        db.commit()
        db.refresh(cat)
    return cat


def get_or_create_unit(db, store_id, name):
    """
    Função auxiliar usada pelo router inventory.py
    """
    if not name:
        return None
    name = name.upper().strip()
    unit = (
        db.query(InventoryUnit)
        .filter(InventoryUnit.store_id == store_id, InventoryUnit.name == name)
        .first()
    )
    if not unit:
        unit = InventoryUnit(store_id=store_id, name=name)
        db.add(unit)
        db.commit()
        db.refresh(unit)
    return unit

# --- NOVA FUNÇÃO: ENRIQUECIMENTO DE COMBOS (KDS/IMPRESSÃO) ---
def enrich_order_with_combo_data(db: Session, store_id: int, items: list):
    """
    Varre os itens do pedido. Se encontrar um Combo Fixo sem detalhes,
    busca a configuração no banco e preenche a lista 'details' automaticamente.
    Isso garante que KDS e Impressora recebam os dados prontos.
    """
    if not items: return

    for item in items:
        # Se já tem detalhes (ex: cliente escolheu sabores no iFood), não mexe
        if item.get('details') and len(item.get('details')) > 0:
            continue
            
        # 1. Identifica o Produto
        product = _resolve_product(db, store_id, item, "internal")
        
        # 2. Se for COMBO FIXO (tem itens configurados)
        if product and product.combo_items and len(product.combo_items) > 0:
            generated_details = []
            
            for child_ref in product.combo_items:
                child_id = child_ref.get('product_id')
                qty = float(child_ref.get('qty', 1))
                
                # Busca nome do filho para ficar bonito na tela
                child_prod = db.query(Product).get(child_id)
                child_name = child_prod.name if child_prod else "Item do Combo"
                
                # Gera o objeto de detalhe padrão
                generated_details.append({
                    "type": "info", # 'info' aparece em cinza/branco padrão
                    "text": f"• {int(qty)}x {child_name}",
                    "code": str(child_id)
                })
            
            # 3. Salva no item do pedido
            if generated_details:
                item['details'] = generated_details
                item['type'] = 'combo' # Marca para formatação visual diferenciada
                print(f"✅ [Combo Auto] {product.name}: Detalhes gerados com sucesso.")