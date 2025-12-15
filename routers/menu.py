from fastapi import APIRouter, Request, Depends, Form
from sqlalchemy.orm import Session, joinedload
from fastapi.responses import HTMLResponse, JSONResponse
from typing import Optional
import json
import re

from database import get_db
from models import (
    User,
    Category,
    Product,
    PizzaSize,
    ProductAddon,
    PizzaBaseRecipe,
    Ingredient,
    ProductSizePrice,
    ProductMapping,
    ProductRecipe,
    AddonPrice,
    AddonRecipe,
    DeliveryFee,
    ProductionSector,
)
from dependencies import templates, check_db_auth, get_today_stats, check_role,get_mixed_current_user

router = APIRouter()

# ==========================================
#           GESTÃO DE CARDÁPIO (CRUD)
# ==========================================


@router.get("/admin/menu", response_class=HTMLResponse)
def admin_menu_view(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    # 1. Categorias e Produtos (Com Separação de Combos)
    categories_db = (
        db.query(Category)
        .filter(Category.store_id == current_user.store_id)
        .order_by(Category.order_index)
        .all()
    )

    categories_list = []
    categories_simple_list = []  # Lista simples para o select do modal
    combos_list = []  # Lista separada para a aba Combos

    for cat in categories_db:
        categories_simple_list.append(
            {"id": cat.id, "name": cat.name, "item_count": len(cat.products)}
        )

        prods_in_cat = []
        for prod in cat.products:
            if not prod.is_active:
                continue

            # Formata Mapeamentos (Integração)
            mappings_list = []
            for m in prod.mappings:
                mappings_list.append(
                    {
                        "id": m.id,
                        "integration_type": m.integration_type,
                        "external_code": m.external_code,
                    }
                )

            # Formata Preços por Tamanho (Para Pizzas)
            size_prices_list = []
            # Ordena pelo preço para pegar o "A partir de" (menor preço)
            sorted_prices = sorted(prod.size_prices, key=lambda x: x.price)
            for sp in sorted_prices:
                size_prices_list.append(
                    {
                        "size_id": sp.size_id,
                        "size_name": sp.size.name if sp.size else "Removido",
                        "price": sp.price,
                    }
                )

            # Dados Completos do Produto
            prod_data = {
                "id": prod.id,
                "name": prod.name,
                "description": prod.description,
                "price": prod.price,
                "image_url": prod.image_url,
                "allows_flavors": prod.allows_flavors,
                "max_flavors": prod.max_flavors,
                "is_pizza": prod.is_pizza,
                "category_id": prod.category_id,
                "mappings": mappings_list,
                "size_prices": size_prices_list,
                "config": prod.config or {},  # Configuração de Bordas/Extras permitidos
                "combo_items": prod.combo_items or [],  # Itens do Combo
            }

            # LÓGICA DE SEPARAÇÃO (Combo vs Produto Normal)
            if prod.combo_items and len(prod.combo_items) > 0:
                # Enriquece os itens do combo com nomes para exibir no card
                enriched_items = []
                for item in prod.combo_items:
                    # Busca nome do produto filho
                    p_child = db.query(Product).get(item["product_id"])
                    if p_child:
                        enriched_items.append(
                            {"name": p_child.name, "qty": item["qty"]}
                        )

                prod_data["combo_details"] = enriched_items
                combos_list.append(prod_data)
            else:
                prods_in_cat.append(prod_data)

        # Adiciona categoria com seus produtos normais
        categories_list.append(
            {
                "id": cat.id,
                "name": cat.name,
                "products": prods_in_cat,
                "item_count": len(prods_in_cat),
            }
        )

    # 2. Bases de Pizza (Agrupadas por Tamanho + Tipo)
    raw_bases = (
        db.query(PizzaBaseRecipe)
        .filter(PizzaBaseRecipe.store_id == current_user.store_id)
        .all()
    )
    base_groups = {}
    for b in raw_bases:
        if not b.size:
            continue
        key = (b.size.id, b.base_type)
        if key not in base_groups:
            base_groups[key] = {
                "size_id": b.size.id,
                "size_name": b.size.name,
                "base_type": b.base_type,
                "item_count": 0,
            }
        base_groups[key]["item_count"] += 1

    bases_list = sorted(list(base_groups.values()), key=lambda x: x["size_name"])

    # 3. Tamanhos
    sizes_db = (
        db.query(PizzaSize).filter(PizzaSize.store_id == current_user.store_id).all()
    )
    sizes_list = [
        {
            "id": s.id,
            "name": s.name,
            "slug": s.slug,
            "slices": s.slices,
            "multiplier": s.recipe_multiplier,
            "max_flavors": s.max_flavors or 1,
        }
        for s in sizes_db
    ]

    # 4. Ingredientes (Com Unidade de Uso para Ficha Técnica)
    ingredients_db = (
        db.query(Ingredient)
        .filter(Ingredient.store_id == current_user.store_id)
        .order_by(Ingredient.name)
        .all()
    )
    ingredients_list = []
    for ing in ingredients_db:
        # Unidade Principal (Estoque/Compra) - Ex: BISNAGA
        unit_name = ing.unit_rel.name if ing.unit_rel else (ing.unit_legacy or "UN")

        # Unidade de Uso (Baixa/Receita) - Ex: GR
        # Se não tiver unidade de uso definida, usa a principal
        usage_name = ing.usage_unit_rel.name if ing.usage_unit_rel else unit_name

        ingredients_list.append(
            {
                "id": ing.id,
                "name": ing.name,
                "unit": unit_name,  # Envia para referência
                "usage_unit": usage_name,  # ESSA é a que vai aparecer na Ficha Técnica (GR/ML)
                "cost": ing.cost or 0.0,
            }
        )

    # 5. Bordas e Complementos (Carrega Preços e Códigos)
    addons_db = (
        db.query(ProductAddon)
        .filter(ProductAddon.store_id == current_user.store_id)
        .all()
    )
    bordas_list = []
    extras_list = []

    for addon in addons_db:
        prices_data = []
        for p in addon.prices:
            prices_data.append(
                {
                    "id": p.id,  # ID da linha de preço (usado para vincular receita no modal)
                    "size_id": p.size_id,
                    "size_name": p.size.name if p.size else "Padrão",
                    "price": p.price,
                    "code": p.external_code,
                    "valid_categories": addon.valid_categories or [],
                }
            )

        data = {
            "id": addon.id,
            "name": addon.name,
            "type": addon.addon_type,
            "prices": prices_data,
        }

        if addon.addon_type == "edge":
            bordas_list.append(data)
        else:
            extras_list.append(data)

    today_stats = get_today_stats(db, current_user.store_id)

    return templates.TemplateResponse(
        "menu.html",
        {
            "request": request,
            "current_user": current_user,
            "categories": categories_list,
            "categories_manager": categories_simple_list,
            "combos": combos_list,  # Lista de Combos para a aba específica
            "base_groups": bases_list,
            "sizes": sizes_list,
            "ingredients": ingredients_list,
            "bordas": bordas_list,  # Lista de Bordas
            "complementos": extras_list,  # Lista de Extras
            "today_stats": today_stats,
            "store": current_user.store,
        },
    )


# --- NOVA API: DADOS COMPLETOS DO MENU (CORRIGIDA) ---
@router.get("/admin/api/menu/full_data")
def get_menu_full_data(
    db: Session = Depends(get_db), current_user: User = Depends(check_db_auth)
):
    try:
        store_id = current_user.store_id

        # Helper para verificar se é combo (Fixo ou Flexível)
        def is_combo(product):
            # 1. Tem itens fixos?
            has_fixed = product.combo_items and len(product.combo_items) > 0
            # 2. Tem configuração de tipo "flexible"?
            config = product.config or {}
            # Verifica se é do tipo flexível OU se tem regras flexíveis salvas
            is_flexible = config.get('combo_type') == 'flexible' or (config.get('flexible_rules') and len(config.get('flexible_rules')) > 0)
            
            return has_fixed or is_flexible

        # 1. Categorias e Produtos Normais
        categories_db = (
            db.query(Category)
            .filter(Category.store_id == store_id)
            .order_by(Category.order_index)
            .all()
        )
        categories_list = []

        # Função auxiliar para formatar produto
        def format_product(p):
            # SE FOR COMBO, REMOVE DA LISTA DE PRODUTOS
            if is_combo(p): return None

            size_prices = []
            if p.size_prices:
                size_prices = [
                    {"size_name": sp.size.name, "price": sp.price}
                    for sp in p.size_prices
                    if sp.size
                ]

            integrations = [m.integration_type for m in p.mappings]

            return {
                "id": p.id,
                "name": p.name,
                "image_url": p.image_url,
                "price": p.price,
                "is_pizza": p.is_pizza,
                "size_prices": size_prices,
                "integrations": integrations,
                "mappings": [
                    {"integration_type": m.integration_type} for m in p.mappings
                ],
                "full_data": {
                    "id": p.id,
                    "name": p.name,
                    "description": p.description,
                    "category_id": p.category_id,
                    "price": p.price,
                    "image_url": p.image_url,
                    "is_pizza": p.is_pizza,
                    "allows_flavors": p.allows_flavors,
                    "max_flavors": getattr(p, "max_flavors", 1),
                    "preparation_method": getattr(p, "preparation_method", ""),
                    "size_prices": [
                        {"size_id": sp.size_id, "price": sp.price}
                        for sp in p.size_prices
                    ],
                    "mappings": [
                        {"source": m.integration_type, "code": m.external_code}
                        for m in p.mappings
                    ],
                    "config": getattr(p, "config", {}) or {},
                },
            }

        # Processa Categorias
        for cat in categories_db:
            prods = []
            for p in cat.products:
                if not p.is_active: continue
                formatted = format_product(p)
                if formatted: prods.append(formatted)
            
            categories_list.append({
                "id": cat.id,
                "name": cat.name,
                "products": prods,
                "item_count": len(prods),
            })

        # RECUPERAÇÃO DE ÓRFÃOS (Produtos sem categoria)
        orphans = db.query(Product).filter(
            Product.store_id == store_id,
            Product.is_active == True,
            Product.category_id == None,
        ).all()

        if orphans:
            orphan_prods = []
            for p in orphans:
                formatted = format_product(p)
                if formatted: orphan_prods.append(formatted)

            if orphan_prods:
                categories_list.insert(0, {
                    "id": -1,
                    "name": "⚠️ Sem Categoria (Recuperados)",
                    "products": orphan_prods,
                    "item_count": len(orphan_prods),
                })

        # 2. COMBOS (Lista Unificada Fixos + Flexíveis)
        combos_list = []
        try:
            # Busca TODOS os produtos e filtra apenas os que são combos
            all_products = db.query(Product).filter(Product.store_id == store_id, Product.is_active == True).all()
            
            for c in all_products:
                if not is_combo(c): continue 
                
                details = []
                
                # Se for Fixo (Lista de itens simples)
                if c.combo_items:
                    # combo_items é uma lista de dicts: [{'product_id': 1, 'qty': 1}]
                    for item in c.combo_items:
                        child = db.query(Product).get(item.get("product_id"))
                        if child: 
                            details.append({"name": child.name, "qty": item.get("qty")})
                
                # Se for Flexível (Lista de Regras)
                config = c.config or {}
                if config.get('combo_type') == 'flexible' or config.get('flexible_rules'):
                    rules = config.get('flexible_rules', [])
                    for r in rules:
                        # CORREÇÃO AQUI: Acesso via dicionário r['name']
                        rule_name = r.get('name', 'Escolha')
                        rule_max = r.get('max_qty', 1)
                        details.append({"name": f"Escolha: {rule_name}", "qty": rule_max})

                combos_list.append({
                    "id": c.id,
                    "name": c.name,
                    "description": c.description,
                    "price": c.price,
                    "image_url": c.image_url,
                    "combo_details": details,
                    "full_data": {
                        "id": c.id,
                        "name": c.name,
                        "description": c.description,
                        "price": c.price,
                        "image_url": c.image_url,
                        "combo_items": c.combo_items,
                        "config": c.config,
                        "mappings": [
                            {"source": m.integration_type, "code": m.external_code}
                            for m in c.mappings
                        ],
                    },
                })
        except Exception as e:
            print(f"❌ Erro ao processar combos: {e}")
            # Continua mesmo se der erro em um combo para não travar tudo

        # 3. Bordas e Extras (Mantido Resumido para economizar espaço, a lógica é padrão)
        bordas = []
        extras = []
        addons_db = db.query(ProductAddon).filter(ProductAddon.store_id == store_id).all()
        for a in addons_db:
            valid_cats = getattr(a, "valid_categories", []) or []
            prices = [{"size_name": p.size.name if p.size else "Padrão", "price": p.price, "code": p.external_code} for p in a.prices]
            item = {
                "id": a.id, "name": a.name, "type": a.addon_type, "prices": prices, "valid_categories": valid_cats,
                "full_data": {"id": a.id, "name": a.name, "type": a.addon_type, "valid_categories": valid_cats, "prices": [{"size_id": p.size_id, "price": p.price, "code": p.external_code, "id": p.id} for p in a.prices]}
            }
            if a.addon_type == "edge": bordas.append(item)
            else: extras.append(item)

        sizes_db = db.query(PizzaSize).filter(PizzaSize.store_id == store_id).all()
        sizes = [{"id": s.id, "name": s.name, "slices": s.slices, "multiplier": s.recipe_multiplier, "max_flavors": getattr(s, "max_flavors", 1)} for s in sizes_db]

        bases_db = db.query(PizzaBaseRecipe).filter(PizzaBaseRecipe.store_id == store_id).all()
        base_groups = {}
        for b in bases_db:
            if not b.size: continue
            key = f"{b.size.id}-{b.base_type}"
            if key not in base_groups: base_groups[key] = {"size_id": b.size.id, "size_name": b.size.name, "base_type": b.base_type, "count": 0}
            base_groups[key]["count"] += 1

        ingredients_list = []
        try:
            ing_db = db.query(Ingredient).filter(Ingredient.store_id == store_id).order_by(Ingredient.name).all()
            for i in ing_db:
                unit = i.usage_unit_rel.name if i.usage_unit_rel else (i.unit_rel.name if i.unit_rel else (i.unit_legacy or "UN"))
                ingredients_list.append({"id": i.id, "name": i.name, "unit": unit})
        except: pass

        return {
            "categories": categories_list,
            "combos": combos_list,
            "bordas": bordas,
            "extras": extras,
            "sizes": sizes,
            "bases": list(base_groups.values()),
            "ingredients": ingredients_list,
        }

    except Exception as e:
        print(f"❌ MENU DATA ERROR: {str(e)}")
        return JSONResponse(status_code=500, content={"message": str(e)})


@router.get("/api/menu/{store_slug}")
def get_digital_menu(store_slug: str, db: Session = Depends(get_db)):
    store = db.query(Store).filter(Store.slug == store_slug).first()
    if not store:
        return JSONResponse(status_code=404, content={"error": "Loja não encontrada"})

    categories = (
        db.query(Category)
        .filter(Category.store_id == store.id, Category.is_active == True)
        .order_by(Category.order_index)
        .all()
    )

    menu_structure = []
    for cat in categories:
        products = (
            db.query(Product)
            .filter(Product.category_id == cat.id, Product.is_active == True)
            .all()
        )
        if not products:
            continue

        prods_data = []
        for p in products:
            prods_data.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "description": p.description,
                    "price": p.price,
                    "image": p.image_url,
                    "config": {
                        "allows_flavors": p.allows_flavors,
                        "max_flavors": p.max_flavors,
                    },
                }
            )
        menu_structure.append({"category_name": cat.name, "items": prods_data})

    return {
        "store": {
            "name": store.name,
            "whatsapp": store.whatsapp_number,
            "pixel_id": store.fb_pixel_id,
        },
        "menu": menu_structure,
    }


# --- ROTA QUE FALTA PARA O PDV FUNCIONAR ---
@router.get("/admin/api/pdv/data")
def get_pdv_data(
    db: Session = Depends(get_db), current_user: User = Depends(get_mixed_current_user)
):
    store_id = current_user.store_id

    # 1. Busca Produtos
    products = (
        db.query(Product)
        .options(
            joinedload(Product.size_prices),
            joinedload(Product.mappings),
            joinedload(Product.recipe_items)
            # REMOVIDO: joinedload(Product.config) -> Causa erro pois é coluna, não relação
        )
        .filter(Product.store_id == store_id, Product.is_active == True)
        .all()
    )

    # PRE-LOAD DOS ADDONS
    all_addons = (
        db.query(ProductAddon)
        .options(joinedload(ProductAddon.prices))
        .filter(ProductAddon.store_id == store_id, ProductAddon.is_active == True)
        .all()
    )

    prods_data = []
    for p in products:
        size_prices = {sp.size_id: sp.price for sp in p.size_prices}

        # --- NOVA LÓGICA DE HERANÇA ---
        config = p.config or {}
        allowed_ids = config.get("allowed_addons", [])
        blocked_ids = config.get("blocked_addons", [])

        valid_addons = []
        for addon in all_addons:
            is_linked_by_cat = p.category_id in (addon.valid_categories or [])
            if (is_linked_by_cat and addon.id not in blocked_ids) or (addon.id in allowed_ids):
                valid_addons.append(addon.id)
        
        ingredients_list = []
        for r in p.recipe_items:
            if r.ingredient:
                ingredients_list.append({"id": r.ingredient.id, "name": r.ingredient.name})

        # --- CORREÇÃO DO FILTRO (MANTIDA) ---
        # Concatena códigos para busca
        codes = [m.external_code for m in p.mappings if m.external_code]
        search_codes = " ".join(codes)
        
        # Garante descrição segura
        desc_safe = p.description or ""
        
        # Cria um campo "search_text" unificado
        search_text = f"{p.name} {desc_safe} {search_codes}"
        # ------------------------------------

        # --- INÍCIO DA CORREÇÃO: ADICIONANDO combo_items ---
        prods_data.append(
            {
                "id": p.id,
                "name": p.name,
                "description": desc_safe,     
                "code": search_codes,         
                "search_text": search_text,
                "price": p.price or 0.0,
                "is_pizza": p.is_pizza,
                "category_id": p.category_id,
                "allows_flavors": p.allows_flavors,
                "max_flavors": p.max_flavors,
                "size_prices": size_prices,
                "valid_addons": valid_addons,
                "ingredients": ingredients_list,
                "config": p.config or {},
                "combo_items": p.combo_items or [], # <--- LINHA CRÍTICA ADICIONADA
            }
        )
        # --- FIM DA CORREÇÃO ---

    # Busca categorias para o filtro
    categories = (
        db.query(Category)
        .filter(Category.store_id == store_id, Category.is_active == True)
        .order_by(Category.order_index)
        .all()
    )
    categories_data = [{"id": c.id, "name": c.name} for c in categories]

    # 2. Busca Tamanhos
    sizes = db.query(PizzaSize).filter(PizzaSize.store_id == store_id).all()
    sizes_data = [
        {"id": s.id, "name": s.name, "slices": s.slices, "max_flavors": s.max_flavors}
        for s in sizes
    ]

    # 3. Busca Bordas e Extras
    addons = (
        db.query(ProductAddon)
        .filter(ProductAddon.store_id == store_id, ProductAddon.is_active == True)
        .all()
    )
    addons_data = []
    for a in addons:
        prices = {ap.size_id: ap.price for ap in a.prices}
        addons_data.append(
            {
                "id": a.id,
                "name": a.name,
                "type": a.addon_type,
                "prices": prices,
                "valid_categories": a.valid_categories or [],
            }
        )

    # 4. Busca Taxas
    fees_data = {}
    try:
        fees = db.query(DeliveryFee).filter(DeliveryFee.store_id == store_id).all()
        for f in fees:
            if f.neighborhood:
                fees_data[f.neighborhood.lower()] = f.fee
    except:
        pass

    return {
        "products": prods_data,
        "categories": categories_data,
        "sizes": sizes_data,
        "addons": addons_data,
        "fees": fees_data,
    }


@router.post("/admin/menu/category/save")
def save_category(
    cat_id: Optional[int] = Form(None),
    name: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    try:
        if not cat_id:
            cat = Category(name=name, store_id=current_user.store_id)
            db.add(cat)
        else:
            cat = db.query(Category).get(cat_id)
            if not cat or cat.store_id != current_user.store_id:
                return JSONResponse(
                    status_code=403, content={"message": "Sem permissão"}
                )
            cat.name = name
        db.commit()
        return {"success": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"message": str(e)})


@router.delete("/admin/menu/category/{cat_id}")
def delete_category(
    cat_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    # 1. Busca a Categoria
    cat = db.query(Category).get(cat_id)
    if not cat or cat.store_id != current_user.store_id:
        return JSONResponse(
            status_code=403, content={"message": "Erro de permissão ou não encontrada"}
        )

    # 2. TRAVA DE SEGURANÇA (NOVO)
    # Verifica se existe pelo menos 1 produto ativo ou inativo nesta categoria
    has_products = db.query(Product).filter(Product.category_id == cat_id).first()

    if has_products:
        return JSONResponse(
            status_code=400,
            content={
                "message": "⛔ PROTEÇÃO: Esta categoria contém produtos! Mova os produtos para outra categoria antes de excluir."
            },
        )

    try:
        db.delete(cat)
        db.commit()
        return {"success": True}
    except Exception as e:
        db.rollback()
        return JSONResponse(
            status_code=500, content={"message": f"Erro ao excluir: {str(e)}"}
        )


@router.post("/admin/menu/product/save")
def save_product(
    prod_id: Optional[int] = Form(None),
    category_id: Optional[int] = Form(None),
    name: str = Form(...),
    description: str = Form(None),
    price: Optional[float] = Form(0.0),
    allows_flavors: bool = Form(False),
    max_flavors: int = Form(1),
    is_pizza: bool = Form(False),
    image_url: str = Form(None),
    size_prices_json: str = Form(None),
    integration_codes_json: str = Form(None),
    combo_items_json: str = Form(None),
    config_json: str = Form(None),
    preparation_method: str = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    try:
        
        # Remove qualquer tag <...> do nome e descrição para salvar apenas texto puro
        clean_name = re.sub(r'<[^>]*>', '', name).strip()
        
        clean_description = None
        if description:
            clean_description = re.sub(r'<[^>]*>', '', description).strip()
        # ----------------------------------------------------------

        # 1. Tratamento de Categoria (Segurança)
        final_category_id = category_id
        if not final_category_id:
            # Se não enviou categoria, busca ou cria a "Geral"
            cat = db.query(Category).filter(
                Category.store_id == current_user.store_id, 
                Category.name == "Geral"
            ).first()
            
            if not cat:
                cat = Category(store_id=current_user.store_id, name="Geral")
                db.add(cat)
                db.flush()
            final_category_id = cat.id

        # 2. Salva/Atualiza Produto Base
        if not prod_id:
            prod = Product(
                store_id=current_user.store_id,
                category_id=final_category_id,
                name=clean_name,
                description=clean_description,
                price=price,
                allows_flavors=allows_flavors,
                max_flavors=max_flavors,
                is_pizza=is_pizza,
                image_url=image_url,
            )
            db.add(prod)
            db.flush()
        else:
            prod = db.query(Product).get(prod_id)
            
            # --- CORREÇÃO AQUI ---
            if not prod or prod.store_id != current_user.store_id:
                return JSONResponse(status_code=403, content={"message": "Erro permissão"})
            # ---------------------

            prod.name = clean_name
            prod.description = clean_description
            prod.price = price
            prod.allows_flavors = allows_flavors
            prod.max_flavors = max_flavors
            prod.is_pizza = is_pizza
            prod.image_url = image_url
            prod.preparation_method = preparation_method
            prod.category_id = final_category_id

        # 3. Salva Itens do Combo (COM GERAÇÃO AUTOMÁTICA DE DESCRIÇÃO)
        if combo_items_json:
            try:
                items_data = json.loads(combo_items_json)
                prod.combo_items = items_data
                prod.is_pizza = False
                
                # --- AUTO-GERAR DESCRIÇÃO VISUAL ---
                # Se for combo fixo, cria a lista bonita para o Cardápio/App
                if items_data and len(items_data) > 0:
                    desc_lines = []
                    for item in items_data:
                        child_id = int(item.get('product_id'))
                        qty = int(item.get('qty', 1))
                        
                        # Busca nome do produto filho
                        child_p = db.query(Product).get(child_id)
                        if child_p:
                            desc_lines.append(f"• {qty}x {child_p.name}")
                    
                    # Sobrescreve a descrição com a lista gerada
                    if desc_lines:
                        prod.description = "\n".join(desc_lines)
                # -----------------------------------

            except Exception as e:
                print(f"Erro combo: {e}")

        # 4. Salva Configurações (Bordas/Extras e AGORA COMBOS FLEXÍVEIS)
        if config_json:
            try:
                new_config = json.loads(config_json)
                
                # Mescla com o que já existe para não perder dados
                current_config = dict(prod.config) if prod.config else {}
                current_config.update(new_config)
                
                prod.config = current_config
            except Exception as e:
                print(f"Erro config: {e}")

        # 5. Salva Preços por Tamanho
        if size_prices_json:
            try:
                prices_data = json.loads(size_prices_json)
                db.query(ProductSizePrice).filter(ProductSizePrice.product_id == prod.id).delete()
                for item in prices_data:
                    sp = ProductSizePrice(
                        product_id=prod.id,
                        size_id=int(item["size_id"]),
                        price=float(item["price"]),
                        is_active=True,
                    )
                    db.add(sp)
            except Exception as e:
                print(f"Erro preços: {e}")

        # 6. Salva Integrações
        if integration_codes_json:
            try:
                codes_data = json.loads(integration_codes_json)
                db.query(ProductMapping).filter(ProductMapping.product_id == prod.id).delete()
                for code_item in codes_data:
                    source = code_item["source"]
                    code = code_item["code"]
                    if not code: continue
                    db.add(ProductMapping(
                        store_id=current_user.store_id,
                        product_id=prod.id,
                        integration_type=source,
                        external_code=code,
                    ))
            except Exception as e:
                print(f"Erro integração: {e}")

        db.commit()
        return {"success": True}
    except Exception as e:
        db.rollback()
        print(f"❌ Erro Crítico Salvar Produto: {str(e)}")
        return JSONResponse(status_code=500, content={"message": str(e)})


@router.delete("/admin/menu/product/{prod_id}")
def delete_product(
    prod_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    # 1. Busca o produto
    prod = db.query(Product).get(prod_id)

    # 2. Verifica segurança (Se pertence à loja do usuário)
    if not prod or prod.category.store_id != current_user.store_id:
        return JSONResponse(
            status_code=403, content={"message": "Sem permissão ou não encontrado."}
        )

    try:
        # 3. Limpeza de dependências (Cascata Manual para garantir)
        # Remove preços de tamanho
        db.query(ProductSizePrice).filter(
            ProductSizePrice.product_id == prod.id
        ).delete()

        # Remove ficha técnica
        db.query(ProductRecipe).filter(ProductRecipe.product_id == prod.id).delete()

        # Remove mapeamentos de integração
        db.query(ProductMapping).filter(ProductMapping.product_id == prod.id).delete()

        # 4. Remove o Produto
        db.delete(prod)
        db.commit()
        return {"success": True}

    except Exception as e:
        db.rollback()
        return JSONResponse(
            status_code=500, content={"message": f"Erro ao excluir: {str(e)}"}
        )


@router.post("/admin/menu/products/bulk-action")
def bulk_product_action(
    action: str = Form(...),  # 'delete' ou 'move_category'
    product_ids: str = Form(...),  # IDs separados por vírgula "1,2,5"
    target_category_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    try:
        ids_list = [int(id) for id in product_ids.split(",")]
        if not ids_list:
            return {"success": False, "message": "Nenhum produto selecionado."}

        products = (
            db.query(Product)
            .filter(Product.store_id == current_user.store_id, Product.id.in_(ids_list))
            .all()
        )

        count = 0
        if action == "delete":
            for p in products:
                # Limpeza segura (mesma lógica do delete individual)
                db.query(ProductSizePrice).filter(
                    ProductSizePrice.product_id == p.id
                ).delete()
                db.query(ProductRecipe).filter(
                    ProductRecipe.product_id == p.id
                ).delete()
                db.query(ProductMapping).filter(
                    ProductMapping.product_id == p.id
                ).delete()
                db.delete(p)
                count += 1

        elif action == "move_category":
            if not target_category_id:
                return JSONResponse(
                    status_code=400, content={"message": "Categoria destino inválida."}
                )

            # Verifica se a categoria pertence à loja
            cat = db.query(Category).get(target_category_id)
            if not cat or cat.store_id != current_user.store_id:
                return JSONResponse(
                    status_code=403, content={"message": "Categoria inválida."}
                )

            for p in products:
                p.category_id = target_category_id
                count += 1

        db.commit()
        return {"success": True, "message": f"{count} produtos processados."}

    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"message": str(e)})


# ==========================================
#        API: tamanhos DE PIZZA (CRUD COMPLETO)
# ==========================================


@router.post("/admin/menu/size/save")
def save_pizza_size(
    size_id: Optional[int] = Form(None),
    name: str = Form(...),
    slices: int = Form(...),
    multiplier: float = Form(...),
    max_flavors: int = Form(1),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    if size_id:
        # --- MODO EDIÇÃO ---
        size = db.query(PizzaSize).get(size_id)
        if not size or size.store_id != current_user.store_id:
            return JSONResponse(status_code=403, content={"message": "Sem permissão"})

        # Atualiza os campos do objeto existente
        size.name = name
        size.slices = slices
        size.recipe_multiplier = multiplier
        size.max_flavors = max_flavors
    else:
        # --- MODO CRIAÇÃO ---
        # Cria um novo objeto já com os dados
        size = PizzaSize(
            store_id=current_user.store_id,
            name=name,
            slices=slices,
            recipe_multiplier=multiplier,
            max_flavors=max_flavors,
        )
        db.add(size)

    db.commit()
    return {"success": True}


@router.delete("/admin/menu/size/{size_id}")
def delete_pizza_size(
    size_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    size = db.query(PizzaSize).get(size_id)
    if not size or size.store_id != current_user.store_id:
        return JSONResponse(status_code=403, content={"message": "Sem permissão"})

    try:
        db.delete(size)
        db.commit()
        return {"success": True}
    except Exception as e:
        db.rollback()
        return JSONResponse(
            status_code=500,
            content={
                "message": "Erro ao excluir (Pode estar em uso nas fichas técnicas)."
            },
        )


@router.post("/admin/menu/base/save")
def save_pizza_base(
    base_id: Optional[int] = Form(None),
    size_slug: Optional[str] = Form(None),  # Mantido para compatibilidade
    size_id: Optional[int] = Form(None),  # <--- PRIORIDADE MÁXIMA
    base_type: str = Form(...),
    ingredient_id: int = Form(...),
    quantity: float = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    # 1. Resolve o ID do Tamanho (Prioriza o ID direto, fallback para slug)
    final_size_id = None

    if size_id:
        final_size_id = size_id
    elif size_slug:
        size_obj = (
            db.query(PizzaSize)
            .filter(
                PizzaSize.store_id == current_user.store_id, PizzaSize.slug == size_slug
            )
            .first()
        )
        if size_obj:
            final_size_id = size_obj.id

    # Se não achou o tamanho, não pode salvar (pois ficaria órfão e não apareceria na tela)
    if not final_size_id:
        return JSONResponse(
            status_code=400,
            content={"message": "Erro: Tamanho de pizza não identificado."},
        )

    # 2. Busca ou Cria (Upsert)
    query = db.query(PizzaBaseRecipe).filter(
        PizzaBaseRecipe.store_id == current_user.store_id,
        PizzaBaseRecipe.size_id == final_size_id,  # Usa o ID resolvido
        PizzaBaseRecipe.base_type == base_type,
        PizzaBaseRecipe.ingredient_id == ingredient_id,
    )

    base = query.first()

    if base:
        # Atualiza existente
        base.quantity = quantity
        # Garante que o slug esteja preenchido caso tenha vindo vazio
        if size_slug:
            base.size_slug = size_slug
    else:
        # Cria novo item na base
        base = PizzaBaseRecipe(
            store_id=current_user.store_id,
            size_slug=size_slug or "",  # Pode ser vazio se tiver ID
            size_id=final_size_id,  # Vínculo Forte
            base_type=base_type,
            ingredient_id=ingredient_id,
            quantity=quantity,
        )
        db.add(base)

    db.commit()
    return {"success": True}


# 1. ROTA PARA LISTAR ITENS NO MODAL
@router.get("/admin/menu/base/{size_id}/{base_type}/items")
def get_base_items(
    size_id: int,
    base_type: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    items = (
        db.query(PizzaBaseRecipe)
        .filter(
            PizzaBaseRecipe.store_id == current_user.store_id,
            PizzaBaseRecipe.size_id == size_id,
            PizzaBaseRecipe.base_type == base_type,
        )
        .all()
    )

    data = []
    for item in items:
        ing_name = item.ingredient.name if item.ingredient else "Item Excluído"

        # --- LÓGICA CORRIGIDA: Prioriza Unidade de Uso ---
        unit_show = "UN"
        if item.ingredient:
            if item.ingredient.usage_unit_rel:
                unit_show = item.ingredient.usage_unit_rel.name  # Ex: GR
            elif item.ingredient.unit_rel:
                unit_show = item.ingredient.unit_rel.name  # Ex: BISNAGA (Fallback)
            else:
                unit_show = item.ingredient.unit_legacy or "UN"

        data.append(
            {
                "id": item.id,
                "ingredient_name": ing_name,
                "ingredient_unit": unit_show,  # Agora manda 'GR' para a tabela
                "quantity": item.quantity,
            }
        )
    return data


# 2. ROTA PARA ATUALIZAR QUANTIDADE (LÁPIS)
@router.post("/admin/menu/base/{base_id}/update_quantity")
def update_base_quantity(
    base_id: int,
    quantity: float = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    base = db.query(PizzaBaseRecipe).get(base_id)
    if not base or base.store_id != current_user.store_id:
        return JSONResponse(status_code=403, content={"message": "Erro"})

    base.quantity = quantity
    db.commit()
    return {"success": True}


# 3. ROTA PARA EXCLUIR ITEM INDIVIDUAL (LIXEIRA DENTRO DO MODAL)
@router.delete("/admin/menu/base/{base_id}")
def delete_base_item(
    base_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    base_item = db.query(PizzaBaseRecipe).get(base_id)
    if not base_item or base_item.store_id != current_user.store_id:
        return JSONResponse(status_code=403, content={"message": "Sem permissão"})

    try:
        db.delete(base_item)
        db.commit()
        return {"success": True}
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"message": str(e)})


# 4. ROTA PARA EXCLUIR GRUPO INTEIRO (LIXEIRA NO CARTÃO)
@router.delete("/admin/menu/base/group/{size_id}/{base_type}")
def delete_base_group(
    size_id: int,
    base_type: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    db.query(PizzaBaseRecipe).filter(
        PizzaBaseRecipe.store_id == current_user.store_id,
        PizzaBaseRecipe.size_id == size_id,
        PizzaBaseRecipe.base_type == base_type,
    ).delete()

    db.commit()
    return {"success": True}


@router.get("/admin/maintenance/fix-ghost-bases")
def fix_ghost_bases(
    db: Session = Depends(get_db), current_user: User = Depends(check_role(["owner"]))
):
    """
    ROTA DE CURA:
    1. Encontra bases sem vínculo de tamanho (size_id NULL).
    2. Tenta recuperar o vínculo usando o slug antigo.
    3. Se não achar o tamanho pai, exclui a base órfã (o "fantasma").
    """
    all_bases = (
        db.query(PizzaBaseRecipe)
        .filter(PizzaBaseRecipe.store_id == current_user.store_id)
        .all()
    )

    fixed_count = 0
    deleted_count = 0

    for base in all_bases:
        should_delete = False

        # CASO 1: Base com ID de tamanho, mas o tamanho não existe mais no banco
        if base.size_id:
            parent_size = db.query(PizzaSize).get(base.size_id)
            if not parent_size:
                should_delete = True

        # CASO 2: Base antiga sem ID (Legado), tenta achar pelo nome (slug)
        else:
            if base.size_slug:
                found_size = (
                    db.query(PizzaSize)
                    .filter(
                        PizzaSize.store_id == current_user.store_id,
                        PizzaSize.slug == base.size_slug,
                    )
                    .first()
                )

                if found_size:
                    base.size_id = found_size.id  # Conserta o vínculo
                    fixed_count += 1
                else:
                    should_delete = True  # Tamanho antigo nem existe mais
            else:
                should_delete = True  # Sem ID e sem Slug = Lixo

        if should_delete:
            db.delete(base)
            deleted_count += 1

    db.commit()

    return {
        "status": "success",
        "message": f"Limpeza concluída! 👻 Fantasmas excluídos: {deleted_count}. 🔧 Vínculos reparados: {fixed_count}.",
        "detail": "Tente excluir o ingrediente novamente agora.",
    }


# 2. NOVA ROTA: SALVAR ADICIONAL (Borda/Extra)
@router.post("/admin/menu/addon/save")
def save_addon(
    addon_id: Optional[int] = Form(None),
    name: str = Form(...),
    addon_type: str = Form(...),
    valid_categories_json: str = Form(None),  # <--- NOVO
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    import json

    valid_cats = []
    if valid_categories_json:
        try:
            valid_cats = json.loads(valid_categories_json)
        except:
            pass

    if addon_id:
        addon = db.query(ProductAddon).get(addon_id)
        if not addon or addon.store_id != current_user.store_id:
            return JSONResponse(status_code=403, content={"error": "Erro"})
        addon.name = name
        addon.addon_type = addon_type
        addon.valid_categories = valid_cats  # Atualiza
    else:
        addon = ProductAddon(
            store_id=current_user.store_id,
            name=name,
            addon_type=addon_type,
            valid_categories=valid_cats,
        )
        db.add(addon)
    db.commit()
    return {"success": True}


# 3. NOVA ROTA: SALVAR PREÇO/CONFIG DO ADICIONAL
@router.post("/admin/menu/addon/price/save")
def save_addon_price(
    addon_id: int = Form(...),
    prices_json: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    try:
        data = json.loads(prices_json)

        for item in data:
            # Tratamento seguro para size_id
            raw_size = item.get("size_id")
            size_id = (
                int(raw_size)
                if raw_size and str(raw_size) != "null" and str(raw_size) != ""
                else None
            )

            price = float(item["price"])
            code = item.get("code", "")

            # Agora com AddonPrice importado, isso vai funcionar:
            ap = (
                db.query(AddonPrice)
                .filter(AddonPrice.addon_id == addon_id, AddonPrice.size_id == size_id)
                .first()
            )

            if ap:
                ap.price = price
                ap.external_code = code
            else:
                ap = AddonPrice(
                    addon_id=addon_id, size_id=size_id, price=price, external_code=code
                )
                db.add(ap)

        db.commit()
        return {"success": True}

    except Exception as e:
        db.rollback()
        print(f"❌ Erro Addon Price: {e}")  # Isso aparecerá no log se der erro de novo
        return JSONResponse(status_code=500, content={"message": str(e)})


# 2. ROTAS DE FICHA TÉCNICA DE ADICIONAL (BORDA/EXTRA)
@router.get("/admin/menu/addon/{price_id}/recipes")
def get_addon_recipes(
    price_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    # Busca receitas vinculadas a um PREÇO específico (que está vinculado a um tamanho)
    recipes = db.query(AddonRecipe).filter(AddonRecipe.addon_price_id == price_id).all()
    data = []
    for r in recipes:
        unit_show = (
            r.ingredient.usage_unit_rel.name
            if r.ingredient.usage_unit_rel
            else (r.ingredient.unit_rel.name if r.ingredient.unit_rel else "UN")
        )

        # Custo
        factor = r.ingredient.conversion_factor or 1.0
        cost = (r.quantity / factor) * r.ingredient.cost

        data.append(
            {
                "id": r.id,
                "ingredient_name": r.ingredient.name,
                "quantity": r.quantity,
                "unit": unit_show,
                "cost": cost,
            }
        )
    return data


@router.post("/admin/menu/addon/recipe/save")
def save_addon_recipe(
    price_id: int = Form(...),
    ingredient_id: int = Form(...),
    quantity: float = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    # Verifica segurança (pertence à loja?)
    # ... (Implementar verificação via join se necessário, assumindo ok por enquanto)
    rec = AddonRecipe(
        addon_price_id=price_id, ingredient_id=ingredient_id, quantity=quantity
    )
    db.add(rec)
    db.commit()
    return {"success": True}


# Apaga um item da receita (ficha técnica)
@router.delete("/admin/menu/addon/recipe/{id}")
def delete_addon_recipe(
    id: int, db: Session = Depends(get_db), current_user: User = Depends(check_db_auth)
):
    rec = db.query(AddonRecipe).get(id)
    if rec:
        db.delete(rec)
        db.commit()
    return {"success": True}


# --- ADICIONE ESTA NOVA (PARA O ERRO 404) ---
# Apaga a Borda ou Extra inteira da lista
@router.delete("/admin/menu/addon/{addon_id}")
def delete_addon(
    addon_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    addon = db.query(ProductAddon).get(addon_id)
    if not addon or addon.store_id != current_user.store_id:
        return JSONResponse(
            status_code=403, content={"message": "Sem permissão ou não encontrado."}
        )

    try:
        # Limpa os preços vinculados antes de apagar o addon
        db.query(AddonPrice).filter(AddonPrice.addon_id == addon.id).delete()

        # (Opcional) Se tiver receitas vinculadas aos preços, o banco pode reclamar.
        # O ideal é o delete cascade no banco, mas por segurança no código:
        # db.query(AddonRecipe).filter(AddonRecipe.addon_price_id.in_(...)).delete()

        db.delete(addon)
        db.commit()
        return {"success": True}
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"message": str(e)})


@router.post("/admin/menu/recipe/save")
def save_product_recipe(
    product_id: int = Form(...),
    ingredient_id: int = Form(...),
    quantity: float = Form(...),
    size_id: Optional[int] = Form(None),  # <--- NOVO CAMPO
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    # Verifica permissão
    prod = db.query(Product).get(product_id)
    if not prod or prod.store_id != current_user.store_id:
        return JSONResponse(status_code=403, content={"message": "Erro de permissão"})

    # Busca se já existe essa receita para esse produto + ingrediente + tamanho
    query = db.query(ProductRecipe).filter(
        ProductRecipe.product_id == product_id,
        ProductRecipe.ingredient_id == ingredient_id,
    )

    # Se foi passado um tamanho, filtra por ele. Se não, filtra por NULL (receita genérica)
    if size_id:
        query = query.filter(ProductRecipe.size_id == size_id)
    else:
        query = query.filter(ProductRecipe.size_id == None)

    recipe = query.first()

    if recipe:
        # Atualiza quantidade
        recipe.quantity = quantity
    else:
        # Cria nova
        recipe = ProductRecipe(
            product_id=product_id,
            ingredient_id=ingredient_id,
            quantity=quantity,
            size_id=size_id,  # <--- SALVA O TAMANHO
        )
        db.add(recipe)

    db.commit()
    return {"success": True}


@router.post("/admin/menu/recipe/{recipe_id}/update_quantity")
def update_recipe_quantity(
    recipe_id: int,
    quantity: float = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    # Busca a receita específica pelo ID
    recipe = db.query(ProductRecipe).get(recipe_id)

    # Validação de segurança
    if not recipe or recipe.product.store_id != current_user.store_id:
        return JSONResponse(
            status_code=403,
            content={"message": "Sem permissão ou receita não encontrada."},
        )

    # Atualiza
    recipe.quantity = quantity
    db.commit()

    return {"success": True}


@router.delete("/admin/menu/recipe/{recipe_id}")
def delete_recipe_item(
    recipe_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    # ... (lógica de delete segura) ...
    item = db.query(ProductRecipe).get(recipe_id)
    if item:
        db.delete(item)
        db.commit()
    return {"success": True}


@router.get("/admin/menu/product/{prod_id}/recipes")
def get_product_recipes_list(
    prod_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    prod = db.query(Product).get(prod_id)
    if not prod or prod.store_id != current_user.store_id:
        return []

    recipes = db.query(ProductRecipe).filter(ProductRecipe.product_id == prod.id).all()

    data = []
    for r in recipes:
        size_name = "Padrão (Todos)"
        if r.size:
            size_name = r.size.name

        unit_show = "UN"
        line_cost = 0.0  # Custo desta linha (Ingrediente * Qtd)

        if r.ingredient:
            # 1. Resolve a Unidade de Exibição (Visual)
            if r.ingredient.usage_unit_rel:
                unit_show = r.ingredient.usage_unit_rel.name
            elif r.ingredient.unit_rel:
                unit_show = r.ingredient.unit_rel.name
            else:
                unit_show = r.ingredient.unit_legacy or "UN"

            # 2. CALCULA O CUSTO (A Mágica)
            # Lógica V5: Baixa = Qtd Receita / Fator
            factor = (
                r.ingredient.conversion_factor
                if (
                    r.ingredient.conversion_factor
                    and r.ingredient.conversion_factor > 0
                )
                else 1.0
            )

            # Qtd que sai do estoque (ex: 0.200 KG)
            qty_deducted = r.quantity / factor

            # Custo = Qtd Estoque * Custo Unitário do Estoque
            line_cost = qty_deducted * r.ingredient.cost

        data.append(
            {
                "id": r.id,
                "ingredient_name": r.ingredient.name,
                "ingredient_unit": unit_show,
                "quantity": r.quantity,
                "size_name": size_name,
                "total_cost": line_cost,  # <--- ENVIANDO O VALOR EM R$
            }
        )

    return sorted(data, key=lambda x: x["size_name"])


@router.post("/admin/menu/recipe/copy")
def copy_product_recipe(
    target_product_id: int = Form(...),
    target_size_id: Optional[int] = Form(None),
    source_product_id: int = Form(...),
    source_size_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    # 1. Validações de Segurança
    target_prod = db.query(Product).get(target_product_id)
    if not target_prod or target_prod.store_id != current_user.store_id:
        return JSONResponse(
            status_code=403, content={"message": "Produto alvo inválido"}
        )

    source_prod = db.query(Product).get(source_product_id)
    if not source_prod or source_prod.store_id != current_user.store_id:
        return JSONResponse(
            status_code=403, content={"message": "Produto origem inválido"}
        )

    # 2. Busca as receitas da ORIGEM
    # Se source_size_id for None (Padrão), busca onde size_id é NULL
    # Se for um ID, busca onde é aquele ID.
    source_recipes = (
        db.query(ProductRecipe)
        .filter(
            ProductRecipe.product_id == source_product_id,
            ProductRecipe.size_id == source_size_id,
        )
        .all()
    )

    if not source_recipes:
        return JSONResponse(
            status_code=404,
            content={
                "message": "O produto de origem não tem ingredientes cadastrados neste tamanho."
            },
        )

    # 3. Limpa as receitas do ALVO (para substituir pela cópia limpa)
    # Cuidado: Só apaga do tamanho que estamos editando!
    db.query(ProductRecipe).filter(
        ProductRecipe.product_id == target_product_id,
        ProductRecipe.size_id == target_size_id,
    ).delete(synchronize_session=False)

    # 4. Cria as novas receitas
    for item in source_recipes:
        new_recipe = ProductRecipe(
            product_id=target_product_id,
            size_id=target_size_id,
            ingredient_id=item.ingredient_id,
            quantity=item.quantity,  # Copia a mesma quantidade (usuário edita depois)
        )
        db.add(new_recipe)

    db.commit()
    return {"success": True, "count": len(source_recipes)}


# ==========================================
#           GESTÃO DE SETORES (KDS)
# ==========================================

@router.get("/admin/api/sectors")
def get_sectors(db: Session = Depends(get_db), current_user: User = Depends(check_db_auth)):
    # Retorna setores com suas categorias vinculadas
    sectors = db.query(ProductionSector).filter(ProductionSector.store_id == current_user.store_id).all()
    
    result = []
    for s in sectors:
        # Busca categorias desse setor
        cats = db.query(Category).filter(Category.sector_id == s.id).all()
        cat_list = [{"id": c.id, "name": c.name} for c in cats]
        
        result.append({
            "id": s.id, 
            "name": s.name, 
            "has_expedition": s.has_expedition if hasattr(s, 'has_expedition') else True,
            "categories": cat_list
        })
    return result

@router.post("/admin/api/sectors/save")
def save_sector(
    id: Optional[int] = Form(None),
    name: str = Form(...),
    has_expedition: bool = Form(True), # Define se tem tela de expedição ou finaliza direto
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    if id:
        sector = db.query(ProductionSector).get(id)
        if sector and sector.store_id == current_user.store_id:
            sector.name = name
            # Verifica se a coluna existe antes de gravar (segurança)
            if hasattr(sector, 'has_expedition'):
                sector.has_expedition = has_expedition
    else:
        # Verifica se o modelo suporta o campo novo
        try:
            sector = ProductionSector(store_id=current_user.store_id, name=name, has_expedition=has_expedition)
        except:
            # Fallback para versão antiga do banco
            sector = ProductionSector(store_id=current_user.store_id, name=name)
            
        db.add(sector)
    
    db.commit()
    return {"success": True}

@router.delete("/admin/api/sectors/{sector_id}")
def delete_sector(sector_id: int, db: Session = Depends(get_db), current_user: User = Depends(check_db_auth)):
    # 1. Busca Segura
    sector = db.query(ProductionSector).get(sector_id)
    if not sector or sector.store_id != current_user.store_id:
        return JSONResponse(status_code=403, content={"message": "Setor não encontrado ou sem permissão."})

    try:
        # 2. Desvincula CATEGORIAS (Bulk Update para performance)
        # Em vez de fazer um loop lento, atualizamos tudo de uma vez
        db.query(Category).filter(Category.sector_id == sector.id).update({Category.sector_id: None}, synchronize_session=False)
        
        # 3. Desvincula PRODUTOS (O passo que faltava!)
        # Se houver produtos vinculados diretamente, eles causavam o erro de integridade
        db.query(Product).filter(Product.sector_id == sector.id).update({Product.sector_id: None}, synchronize_session=False)
        
        # 4. Agora sim, exclui o setor com segurança
        db.delete(sector)
        db.commit()
        
        return {"success": True}

    except Exception as e:
        db.rollback() # Desfaz alterações em caso de erro para não travar o banco
        print(f"❌ Erro ao excluir setor: {str(e)}")
        return JSONResponse(status_code=500, content={"message": f"Erro interno: {str(e)}"})

@router.post("/admin/menu/category/link-sector")
def link_category_sector(
    category_id: int = Form(...),
    sector_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth)
):
    cat = db.query(Category).get(category_id)
    if cat and cat.store_id == current_user.store_id:
        cat.sector_id = sector_id if sector_id > 0 else None
        db.commit()
    return {"success": True}
