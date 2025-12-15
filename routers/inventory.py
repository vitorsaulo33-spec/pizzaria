from fastapi import APIRouter, Request, Depends, Form, File, UploadFile, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc, func, or_
from fastapi.responses import HTMLResponse, JSONResponse
from datetime import datetime
from pydantic import BaseModel
import json
from typing import Optional

from database import get_db
from models import (
    User,
    Ingredient,
    InventoryCategory,
    InventoryUnit,
    StockLog,
    ImportedInvoice,
    Bill,
    IngredientRecipe,
    ProductRecipe,
    PizzaBaseRecipe,
    AddonRecipe,
)
from dependencies import templates, check_db_auth, check_role
from services.analytics import PizzaBrain
from services.invoice_reader import extract_data_from_invoice
from services.stock_engine import get_or_create_category, get_or_create_unit

router = APIRouter()


# Modelos Pydantic internos para as rotas de API
class DateRange(BaseModel):
    start: str
    end: str


class ForecastRequest(BaseModel):
    analysis_start: str
    analysis_end: str
    days_to_cover: int


class ProductionRequest(BaseModel):
    ingredient_id: int
    quantity_to_produce: float


class StockMovementRequest(BaseModel):
    ingredient_id: int
    type: str
    quantity: float
    reason: str


@router.get("/admin/inventory", response_class=HTMLResponse)
def inventory_view(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role(["owner", "manager"])),
    search: Optional[str] = None,
    cat_filter: Optional[str] = None,
    status_filter: Optional[str] = None,
):
    # Filtros de Query
    query = db.query(Ingredient).filter(Ingredient.store_id == current_user.store_id)
    if search:
        term = f"%{search}%"
        query = query.filter(
            or_(Ingredient.name.ilike(term), Ingredient.integration_code.ilike(term))
        )
    if cat_filter and cat_filter != "all":
        try:
            query = query.filter(Ingredient.category_id == int(cat_filter))
        except:
            pass
    if status_filter == "low":
        query = query.filter(Ingredient.current_stock <= Ingredient.min_stock)
    elif status_filter == "over":
        query = query.filter(Ingredient.current_stock >= Ingredient.max_stock)

    ingredients_db = query.order_by(Ingredient.name).all()

    # Prepara listas para o Frontend
    ingredients_list = []
    total_stock_value = 0.0
    critical_count = 0
    expiring_soon = []
    now = datetime.now()

    # Busca lista completa para o Select2 (Sem filtros da tabela)
    all_raw = (
        db.query(Ingredient).filter(Ingredient.store_id == current_user.store_id).all()
    )
    select_list = []
    for i in all_raw:
        u = i.unit_rel.name if i.unit_rel else (i.unit_legacy or "UN")
        uu = i.usage_unit_rel.name if i.usage_unit_rel else u
        select_list.append({"id": i.id, "name": i.name, "unit": u, "usage_unit": uu})

    for ing in ingredients_db:
        cat_name = (
            ing.category_rel.name
            if ing.category_rel
            else (ing.category_legacy or "Geral")
        )
        unit_name = ing.unit_rel.name if ing.unit_rel else (ing.unit_legacy or "UN")
        usage_unit_name = ing.usage_unit_rel.name if ing.usage_unit_rel else unit_name

        if ing.current_stock <= ing.min_stock:
            critical_count += 1

        if ing.expiration_date:
            delta = (ing.expiration_date - now).days
            if delta <= 7:
                expiring_soon.append(
                    {
                        "name": ing.name,
                        "days": delta,
                        "date": ing.expiration_date.strftime("%d/%m/%Y"),
                    }
                )

        total_stock_value += ing.current_stock * ing.cost

        ingredients_list.append(
            {
                "id": ing.id,
                "name": ing.name,
                "category_name": cat_name,
                "category_id": ing.category_id,
                "current": ing.current_stock,
                "min": ing.min_stock,
                "max": ing.max_stock,
                "cost": ing.cost,
                "unit": unit_name,
                "unit_id": ing.unit_id,
                "usage_unit": usage_unit_name,
                "usage_unit_id": ing.usage_unit_id,
                "integration_code": ing.integration_code,
                "conversion_factor": ing.conversion_factor,
                "is_available_for_sale": ing.is_available_for_sale,
                "expiration_date": (
                    ing.expiration_date.strftime("%Y-%m-%d")
                    if ing.expiration_date
                    else ""
                ),
                "status_color": (
                    "red"
                    if ing.current_stock <= ing.min_stock
                    else ("yellow" if ing.current_stock >= ing.max_stock else "indigo")
                ),
                "percent": (
                    min(100, max(0, (ing.current_stock / ing.max_stock * 100)))
                    if ing.max_stock > 0
                    else 0
                ),
            }
        )

    return templates.TemplateResponse(
        "inventory.html",
        {
            "request": request,
            "current_user": current_user,
            "ingredients": ingredients_list,
            "all_ingredients_json": select_list,  # Lista limpa para o Select2
            "total_value": total_stock_value,
            "critical_count": critical_count,
            "expiring_list": sorted(expiring_soon, key=lambda x: x["days"]),
            "categories": [
                {"id": c.id, "name": c.name}
                for c in db.query(InventoryCategory)
                .filter(InventoryCategory.store_id == current_user.store_id)
                .all()
            ],
            "units": [
                {"id": u.id, "name": u.name}
                for u in db.query(InventoryUnit)
                .filter(InventoryUnit.store_id == current_user.store_id)
                .all()
            ],
            "filters": {
                "search": search or "",
                "cat": cat_filter or "all",
                "status": status_filter or "all",
            },
        },
    )


# --- API: Salvar Ingrediente (Atualizada para IDs) ---
@router.post("/admin/inventory/ingredient/save")
async def save_ingredient(
    ing_id: Optional[int] = Form(None),
    name: str = Form(...),
    category_id: Optional[int] = Form(None),
    unit_id: Optional[int] = Form(None),
    usage_unit_id: Optional[int] = Form(None),  # <--- (1) Unidade de Receita
    integration_code: Optional[str] = Form(None),
    cost: float = Form(0.0),
    min_stock: float = Form(0.0),
    max_stock: float = Form(0.0),
    current_stock: float = Form(0.0),
    conversion_factor: float = Form(1.0),  # <--- (2) Fator de Conversão
    is_available_for_sale: bool = Form(False),
    expiration_date: str = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    try:
        dt_expiry = None
        if expiration_date:
            try:
                dt_expiry = datetime.strptime(expiration_date, "%Y-%m-%d")
            except:
                pass

        if not ing_id:
            ing = Ingredient(
                store_id=current_user.store_id,
                name=name,
                cost=cost,
                min_stock=min_stock,
                max_stock=max_stock,
                current_stock=current_stock,
                integration_code=integration_code,
                category_id=category_id,
                unit_id=unit_id,
                usage_unit_id=usage_unit_id,  # <--- Salva Unidade Receita
                conversion_factor=conversion_factor,  # <--- Salva Fator
                is_available_for_sale=is_available_for_sale,
                expiration_date=dt_expiry,
            )
            db.add(ing)
        else:
            ing = db.query(Ingredient).get(ing_id)
            if not ing or ing.store_id != current_user.store_id:
                return JSONResponse(
                    status_code=403, content={"message": "Sem permissão"}
                )

            ing.name = name
            ing.cost = cost
            ing.min_stock = min_stock
            ing.max_stock = max_stock
            ing.current_stock = current_stock
            ing.integration_code = integration_code
            ing.category_id = category_id
            ing.unit_id = unit_id
            ing.usage_unit_id = usage_unit_id  # <--- Atualiza Unidade Receita
            ing.conversion_factor = conversion_factor  # <--- Atualiza Fator
            ing.is_available_for_sale = is_available_for_sale
            ing.expiration_date = dt_expiry

        db.commit()
        return {"success": True}
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"message": str(e)})


@router.delete("/admin/inventory/ingredient/{ing_id}")
async def delete_ingredient(
    ing_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    # 1. Busca o ingrediente
    ing = db.query(Ingredient).get(ing_id)
    if not ing or ing.store_id != current_user.store_id:
        return JSONResponse(
            status_code=403, content={"message": "Permissão negada ou não encontrado."}
        )

    # 2. VERIFICAÇÃO DE SEGURANÇA (Dependências)
    # Verifica se está em ficha técnica de produto
    usage_prod = (
        db.query(ProductRecipe).filter(ProductRecipe.ingredient_id == ing.id).first()
    )
    if usage_prod:
        return JSONResponse(
            status_code=400,
            content={
                "message": f"Não é possível excluir: Usado no produto '{usage_prod.product.name}'."
            },
        )

    # Verifica se está em base de pizza
    usage_base = (
        db.query(PizzaBaseRecipe)
        .filter(PizzaBaseRecipe.ingredient_id == ing.id)
        .first()
    )
    if usage_base:
        return JSONResponse(
            status_code=400,
            content={"message": "Não é possível excluir: Usado em uma Base de Pizza."},
        )

    # Verifica se está em adicionais
    usage_addon = (
        db.query(AddonRecipe).filter(AddonRecipe.ingredient_id == ing.id).first()
    )
    if usage_addon:
        return JSONResponse(
            status_code=400,
            content={"message": "Não é possível excluir: Usado em um Adicional/Borda."},
        )

    try:
        # 3. Se passou por tudo, exclui
        db.delete(ing)
        db.commit()
        return {"success": True}

    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"message": str(e)})


@router.post("/admin/inventory/process-invoice-items-financial")
async def process_invoice_financial(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    payload = await request.json()
    items_data = payload.get("items", [])
    invoice_key = payload.get("invoice_key")

    # Debug: Ver o que está chegando no servidor
    print(f"📦 [Financeiro/Estoque] Recebido payload com {len(items_data)} itens.")
    
    # DADOS FINANCEIROS
    payment_method = payload.get("payment_method", "Outros")
    due_date_str = payload.get("due_date")
    is_paid = payload.get("is_paid", False)
    total_invoice_value = float(payload.get("total_value") or 0.0)

    try:
        count_updated = 0
        count_created = 0

        # Garante categorias padrão
        cat_revenda = get_or_create_category(db, current_user.store_id, "Bebidas/Revenda")
        cat_insumo = get_or_create_category(db, current_user.store_id, "Insumos Gerais")

        for i, item in enumerate(items_data):
            # Tratamento de dados mais robusto
            mapped_id = item.get("mapped_id")
            
            # Se mapped_id for string vazia ou "null", força None
            if str(mapped_id).lower() in ['none', 'null', '']:
                mapped_id = None
                
            qty_invoice = float(item.get("qty") or 0)
            cost_invoice = float(item.get("cost") or 0)
            code = item.get("code")
            is_resale = item.get("is_resale", False)
            
            # Garante unidade maiúscula
            unit_str = str(item.get("unit") or "UN").upper().strip()
            unit_obj = get_or_create_unit(db, current_user.store_id, unit_str)

            ing = None
            if mapped_id:
                try:
                    ing = db.query(Ingredient).get(int(mapped_id))
                    # Se não achou (ex: ID de outra loja ou deletado), força criação como novo
                    if not ing or ing.store_id != current_user.store_id:
                        print(f"   ⚠️ Item {i}: ID {mapped_id} não encontrado. Criando como novo.")
                        ing = None
                except:
                    ing = None

            if ing:
                # --- ATUALIZAÇÃO ---
                qty_to_add = qty_invoice
                
                # Custo Médio Ponderado
                total_old = (ing.current_stock or 0) * (ing.cost or 0)
                total_new = qty_to_add * cost_invoice
                new_stock_total = (ing.current_stock or 0) + qty_to_add

                if new_stock_total > 0:
                    ing.cost = (total_old + total_new) / new_stock_total
                else:
                    ing.cost = cost_invoice # Fallback se estoque for zero
                
                ing.current_stock = new_stock_total
                
                if not ing.integration_code and code:
                    ing.integration_code = code
                
                count_updated += 1
            else:
                # --- CRIAÇÃO ---
                target_cat = cat_revenda if is_resale else cat_insumo
                
                new_ing = Ingredient(
                    store_id=current_user.store_id,
                    name=item.get("name_in_invoice") or "Item sem nome",
                    cost=cost_invoice,
                    current_stock=qty_invoice,
                    min_stock=10.0,
                    max_stock=100.0,
                    integration_code=code,
                    unit_id=unit_obj.id,
                    category_id=target_cat.id,
                    conversion_factor=1.0,
                    is_available_for_sale=False,
                )
                db.add(new_ing)
                count_created += 1

        # 2. LANÇA NO FINANCEIRO
        if total_invoice_value > 0:
            due_dt = datetime.strptime(due_date_str, "%Y-%m-%d") if due_date_str else datetime.now()
            paid_dt = datetime.now() if is_paid else None

            bill = Bill(
                store_id=current_user.store_id,
                description=f"NFe {invoice_key[-6:] if invoice_key else 'Entrada'}",
                amount=total_invoice_value,
                due_date=due_dt,
                paid_at=paid_dt,
                payment_method=payment_method,
                invoice_key=invoice_key,
            )
            db.add(bill)
            
        # 3. REGISTRA A NOTA
        if invoice_key:
            exists = db.query(ImportedInvoice).filter_by(access_key=invoice_key).first()
            if not exists:
                db.add(ImportedInvoice(store_id=current_user.store_id, access_key=invoice_key))

        db.commit()
        
        msg = f"Processado! {count_updated} atualizados, {count_created} criados."
        print(f"✅ {msg}")
        return {"success": True, "message": msg}

    except Exception as e:
        db.rollback()
        print(f"❌ Erro Crítico Processamento: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"message": str(e)})


# --- API ATUALIZADA: PROCESSAR NOTA (Com Auto-Create de Categoria/Unidade) ---
@router.post("/admin/inventory/process-invoice-items")
async def process_invoice_items(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    payload = await request.json()
    if isinstance(payload, list):
        data = payload
        invoice_key = None
    else:
        data = payload.get("items", [])
        invoice_key = payload.get("invoice_key")

    try:
        count_updated = 0
        count_created = 0

        # Garante Categoria Padrão e Unidade Padrão
        cat_revenda = get_or_create_category(
            db, current_user.store_id, "Bebidas/Revenda"
        )
        cat_insumo = get_or_create_category(db, current_user.store_id, "Insumos Gerais")

        for item in data:
            mapped_id = item.get("mapped_id")

            # Qtd que veio na nota (ex: 1 CX, 5 KG)
            qty_invoice = float(item.get("qty", 0))

            # Custo unitário da nota
            cost_invoice = float(item.get("cost", 0))

            code = item.get("code")
            is_resale = item.get("is_resale", False)
            unit_str = item.get("unit", "UN")

            # Garante que a unidade exista no banco
            unit_obj = get_or_create_unit(db, current_user.store_id, unit_str)

            if mapped_id:
                # ITEM JÁ EXISTE: Soma direta ao estoque
                ing = db.query(Ingredient).get(mapped_id)
                if ing and ing.store_id == current_user.store_id:

                    # NOVA LÓGICA V5: Estoque é igual a Compra.
                    # Se entrou 1 CX, soma 1.0 ao estoque.
                    qty_to_add = qty_invoice

                    # Cálculo de Custo Médio Ponderado
                    total_old = ing.current_stock * ing.cost
                    total_new = qty_to_add * cost_invoice
                    new_stock_total = ing.current_stock + qty_to_add

                    if new_stock_total > 0:
                        ing.cost = (total_old + total_new) / new_stock_total

                    # Atualiza Estoque
                    ing.current_stock = new_stock_total

                    if not ing.integration_code and code:
                        ing.integration_code = code
                    count_updated += 1
            else:
                # NOVO ITEM: Cria com o estoque inicial da nota
                target_cat = cat_revenda if is_resale else cat_insumo

                new_ing = Ingredient(
                    store_id=current_user.store_id,
                    name=item.get("name_in_invoice"),
                    cost=cost_invoice,
                    current_stock=qty_invoice,  # Estoque inicial = Qtd Nota
                    min_stock=10.0,
                    max_stock=100.0,
                    integration_code=code,
                    unit_id=unit_obj.id,  # Unidade Principal = Unidade da Nota
                    category_id=target_cat.id,
                    conversion_factor=1.0,  # Fator padrão 1.0 (Usuário ajusta depois se precisar)
                    is_available_for_sale=False,
                )
                db.add(new_ing)
                count_created += 1

        if invoice_key:
            exists = db.query(ImportedInvoice).filter_by(access_key=invoice_key).first()
            if not exists:
                db.add(
                    ImportedInvoice(
                        store_id=current_user.store_id, access_key=invoice_key
                    )
                )

        db.commit()
        return {
            "success": True,
            "message": f"Estoque atualizado! {count_updated} itens somados e {count_created} criados.",
        }

    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"message": str(e)})


@router.post("/admin/stock/upload-invoice")
async def upload_invoice_ai(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    if not file:
        return {"error": "Arquivo vazio"}
    contents = await file.read()

    # Extrai dados (Itens + Chave + Total)
    result = extract_data_from_invoice(contents, file.content_type)

    if "error" in result:
        return JSONResponse(status_code=500, content=result)

    invoice_key = result.get("invoice_key")
    warning_msg = None

    if invoice_key:
        exists = (
            db.query(ImportedInvoice)
            .filter(
                ImportedInvoice.store_id == current_user.store_id,
                ImportedInvoice.access_key == invoice_key,
            )
            .first()
        )

        if exists:
            date_str = exists.imported_at.strftime("%d/%m/%Y")
            warning_msg = f"⚠️ Nota já importada em {date_str}."

    return {
        "message": "Leitura concluída!",
        "data": result,  # Aqui dentro vai o total_value e os items
        "invoice_key": invoice_key,
        "warning": warning_msg,
    }


@router.post("/admin/inventory/aux/save")
def save_auxiliary(
    type: str = Form(...),  # 'category' ou 'unit'
    id: Optional[int] = Form(None),  # ID para edição
    name: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    try:
        name_clean = (
            name.strip().title() if type == "category" else name.strip().upper()
        )

        if type == "category":
            if id:
                # Edição
                cat = db.query(InventoryCategory).get(id)
                if not cat or cat.store_id != current_user.store_id:
                    return JSONResponse(status_code=403, content={"message": "Erro"})
                cat.name = name_clean
            else:
                # Criação (Verifica duplicidade)
                exists = (
                    db.query(InventoryCategory)
                    .filter(
                        InventoryCategory.store_id == current_user.store_id,
                        InventoryCategory.name == name_clean,
                    )
                    .first()
                )
                if exists:
                    return JSONResponse(
                        status_code=400, content={"message": "Já existe."}
                    )
                db.add(
                    InventoryCategory(store_id=current_user.store_id, name=name_clean)
                )

        elif type == "unit":
            if id:
                unit = db.query(InventoryUnit).get(id)
                if not unit or unit.store_id != current_user.store_id:
                    return JSONResponse(status_code=403, content={"message": "Erro"})
                unit.name = name_clean
            else:
                exists = (
                    db.query(InventoryUnit)
                    .filter(
                        InventoryUnit.store_id == current_user.store_id,
                        InventoryUnit.name == name_clean,
                    )
                    .first()
                )
                if exists:
                    return JSONResponse(
                        status_code=400, content={"message": "Já existe."}
                    )
                db.add(InventoryUnit(store_id=current_user.store_id, name=name_clean))

        db.commit()
        return {"success": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"message": str(e)})


@router.delete("/admin/inventory/aux/{type}/{id}")
def delete_auxiliary(
    type: str,
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    try:
        if type == "category":
            item = db.query(InventoryCategory).get(id)
            # Verifica uso antes de deletar
            if item.ingredients:
                return JSONResponse(
                    status_code=400,
                    content={
                        "message": "Não pode excluir: Existem ingredientes nesta categoria."
                    },
                )
        elif type == "unit":
            item = db.query(InventoryUnit).get(id)
            if item.ingredients:
                return JSONResponse(
                    status_code=400,
                    content={
                        "message": "Não pode excluir: Existem ingredientes usando esta unidade."
                    },
                )

        if not item or item.store_id != current_user.store_id:
            return JSONResponse(status_code=404, content={"message": "Não encontrado"})

        db.delete(item)
        db.commit()
        return {"success": True}

    except Exception as e:
        return JSONResponse(status_code=500, content={"message": str(e)})


@router.post("/admin/inventory/reports/consumption")
def api_stock_consumption(
    req: DateRange,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    try:
        s = datetime.strptime(req.start, "%Y-%m-%d").replace(hour=0, minute=0, second=0)
        e = datetime.strptime(req.end, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59
        )

        # Consulta SQL poderosa via SQLAlchemy
        # Agrupa logs de SAÍDA (OUT) por ingrediente
        results = (
            db.query(
                Ingredient.name,
                InventoryCategory.name.label("cat_name"),
                InventoryUnit.name.label("unit_name"),
                func.sum(StockLog.quantity).label("total_qty"),
                func.sum(StockLog.quantity * StockLog.cost_at_time).label("total_cost"),
            )
            .join(Ingredient, StockLog.ingredient_id == Ingredient.id)
            .outerjoin(
                InventoryCategory, Ingredient.category_id == InventoryCategory.id
            )
            .outerjoin(InventoryUnit, Ingredient.unit_id == InventoryUnit.id)
            .filter(
                StockLog.store_id == current_user.store_id,
                StockLog.movement_type == "OUT",  # Apenas saídas (consumo)
                StockLog.created_at >= s,
                StockLog.created_at <= e,
            )
            .group_by(
                Ingredient.id,
                Ingredient.name,
                InventoryCategory.name,
                InventoryUnit.name,
            )
            .order_by(desc("total_cost"))
            .all()
        )

        report = []
        for r in results:
            report.append(
                {
                    "name": r.name,
                    "category": r.cat_name or "Geral",
                    "qty": r.total_qty or 0.0,
                    "unit": r.unit_name or "UN",
                    "cost": r.total_cost or 0.0,
                }
            )

        return {"data": report}
    except Exception as e:
        print(f"Erro Relatório: {e}")
        return JSONResponse(status_code=500, content={"message": str(e)})


@router.post(
    "/admin/inventory/reports/forecast"
)  # Mudou de GET para POST para enviar JSON
def api_stock_forecast(
    req: ForecastRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    try:
        s = datetime.strptime(req.analysis_start, "%Y-%m-%d").replace(
            hour=0, minute=0, second=0
        )
        e = datetime.strptime(req.analysis_end, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59
        )

        brain = PizzaBrain(db, current_user.store_id)
        forecast = brain.generate_inventory_forecast(s, e, req.days_to_cover)

        return forecast
    except Exception as e:
        return JSONResponse(status_code=500, content={"message": str(e)})


@router.post("/admin/inventory/produce")
def execute_production(
    req: ProductionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    # 1. Busca o item "pai" (O que está sendo produzido)
    target_item = db.query(Ingredient).get(req.ingredient_id)
    if not target_item or target_item.store_id != current_user.store_id:
        return JSONResponse(status_code=403, content={"message": "Item inválido."})

    # 2. Busca a receita dele
    recipe_items = (
        db.query(IngredientRecipe)
        .filter(IngredientRecipe.parent_ingredient_id == target_item.id)
        .all()
    )

    if not recipe_items:
        return JSONResponse(
            status_code=400,
            content={"message": "Este item não possui receita cadastrada."},
        )

    try:
        history_log = []

        # 3. Executa a Baixa dos Insumos
        for item in recipe_items:
            child = item.child_ingredient

            # --- CORREÇÃO DA MATEMÁTICA DE UNIDADE ---
            # Ex: Receita pede 1000g. Estoque é KG. Fator é 1000.
            # Qtd Bruta = 1000 * 1 (produção) = 1000
            # Baixa Real = 1000 / 1000 = 1 KG.

            raw_qty_needed = item.quantity * req.quantity_to_produce
            factor = (
                child.conversion_factor
                if (child.conversion_factor and child.conversion_factor > 0)
                else 1.0
            )
            real_deduction = raw_qty_needed / factor

            # Atualiza Estoque
            child.current_stock -= real_deduction

            # --- GERA LOG DE CONSUMO (KARDEX) ---
            log_out = StockLog(
                store_id=current_user.store_id,
                ingredient_id=child.id,
                user_name=current_user.full_name,
                movement_type="OUT",  # Saída por produção
                quantity=real_deduction,
                old_stock=child.current_stock + real_deduction,  # O que tinha antes
                new_stock=child.current_stock,
                cost_at_time=child.cost,
                reason=f"Produção de {req.quantity_to_produce} {target_item.name}",
            )
            db.add(log_out)

            unit_name = child.unit_rel.name if child.unit_rel else "UN"
            history_log.append(f"-{real_deduction:.3f} {unit_name} de {child.name}")

        # 4. Adiciona o Estoque no item produzido (Entrada)
        old_target_stock = target_item.current_stock
        target_item.current_stock += req.quantity_to_produce

        log_in = StockLog(
            store_id=current_user.store_id,
            ingredient_id=target_item.id,
            user_name=current_user.full_name,
            movement_type="IN",  # Entrada por produção
            quantity=req.quantity_to_produce,
            old_stock=old_target_stock,
            new_stock=target_item.current_stock,
            cost_at_time=target_item.cost,
            reason="Produção Interna",
        )
        db.add(log_in)

        db.commit()
        return {
            "success": True,
            "message": f"Produção realizada! Estoque atualizado.",
            "log": history_log,
        }

    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"message": str(e)})


# 2. ROTA DE SALVAR RECEITA DE INSUMO
@router.post("/admin/inventory/recipe/save")
def save_ingredient_recipe(
    parent_id: int = Form(...),
    child_id: int = Form(...),
    quantity: float = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    print(
        f"👉 [Receita] Tentando salvar: Pai={parent_id}, Filho={child_id}, Qtd={quantity}"
    )

    # 1. Validação de Loop (Crítica)
    if parent_id == child_id:
        return JSONResponse(
            status_code=400,
            content={
                "message": "ERRO: Você está tentando colocar o produto dentro dele mesmo."
            },
        )

    # 2. Validação de Quantidade
    if quantity <= 0:
        return JSONResponse(
            status_code=400,
            content={"message": "ERRO: A quantidade deve ser maior que zero."},
        )

    try:
        # 3. Busca ou Cria
        exists = (
            db.query(IngredientRecipe)
            .filter(
                IngredientRecipe.parent_ingredient_id == parent_id,
                IngredientRecipe.child_ingredient_id == child_id,
            )
            .first()
        )

        if exists:
            exists.quantity = quantity
        else:
            new_rec = IngredientRecipe(
                parent_ingredient_id=parent_id,
                child_ingredient_id=child_id,
                quantity=quantity,
            )
            db.add(new_rec)

        db.commit()
        return {"success": True, "message": "Item adicionado à receita com sucesso!"}

    except Exception as e:
        db.rollback()
        print(f"❌ Erro Crítico Receita: {e}")
        return JSONResponse(
            status_code=500, content={"message": f"Erro interno: {str(e)}"}
        )


@router.get("/admin/inventory/ingredient/{parent_id}/recipe")
def get_ingredient_recipe_list(
    parent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    recipes = (
        db.query(IngredientRecipe)
        .filter(IngredientRecipe.parent_ingredient_id == parent_id)
        .all()
    )

    data = []
    for r in recipes:
        child = r.child_ingredient

        # Define qual unidade mostrar (Prioridade: Unidade de Uso > Unidade Estoque)
        unit_display = "UN"
        if child.usage_unit_rel:
            unit_display = child.usage_unit_rel.name
        elif child.unit_rel:
            unit_display = child.unit_rel.name
        elif child.unit_legacy:
            unit_display = child.unit_legacy

        data.append(
            {
                "id": r.id,
                "child_name": child.name,
                "qty": r.quantity,
                "unit": unit_display,  # <--- Enviando a unidade correta
            }
        )
    return data


# ROTA PARA EXCLUIR ITEM DA RECEITA (Adicione se não tiver)
@router.delete("/admin/inventory/recipe-item/{item_id}")
def delete_ingredient_recipe_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    item = db.query(IngredientRecipe).get(item_id)
    if item:
        db.delete(item)
        db.commit()
    return {"success": True}


@router.post("/admin/inventory/ingredient/{id}/rename")
def rename_ingredient_recipe(
    id: int,
    name: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    """Renomeia o produto (pai da receita)"""
    ing = db.query(Ingredient).get(id)
    if not ing or ing.store_id != current_user.store_id:
        return JSONResponse(status_code=403, content={"message": "Sem permissão."})

    ing.name = name
    db.commit()
    return {"success": True, "message": "Receita renomeada!"}


@router.delete("/admin/inventory/ingredient/{id}/recipe-clear")
def clear_ingredient_recipe(
    id: int, db: Session = Depends(get_db), current_user: User = Depends(check_db_auth)
):
    """Limpa a ficha técnica (exclui a receita, mantém o produto)"""
    ing = db.query(Ingredient).get(id)
    if not ing or ing.store_id != current_user.store_id:
        return JSONResponse(status_code=403, content={"message": "Sem permissão."})

    # Remove todos os itens da receita onde este ingrediente é o pai
    count = (
        db.query(IngredientRecipe)
        .filter(IngredientRecipe.parent_ingredient_id == id)
        .delete()
    )
    db.commit()
    return {"success": True, "message": f"Receita limpa. {count} itens removidos."}


@router.post("/admin/inventory/transaction")
def save_stock_transaction(
    req: StockMovementRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    ing = db.query(Ingredient).get(req.ingredient_id)
    if not ing or ing.store_id != current_user.store_id:
        return JSONResponse(
            status_code=403, content={"message": "Item não encontrado."}
        )

    old_qty = ing.current_stock
    new_qty = old_qty
    qty_moved = abs(req.quantity)

    # Lógica de Cálculo
    if req.type == "in":
        new_qty = old_qty + qty_moved
    elif req.type == "out":
        new_qty = old_qty - qty_moved
    elif req.type == "adjust":
        # No balanço, a quantidade enviada é o NOVO saldo final
        new_qty = qty_moved
        # Calculamos a diferença para registro
        qty_moved = new_qty - old_qty

    # Atualiza o Ingrediente
    ing.current_stock = new_qty

    # Cria o Log (Kardex)
    log = StockLog(
        store_id=current_user.store_id,
        ingredient_id=ing.id,
        user_name=current_user.full_name,
        movement_type=req.type.upper(),
        quantity=qty_moved,
        old_stock=old_qty,
        new_stock=new_qty,
        cost_at_time=ing.cost,
        reason=req.reason or "Manual",
    )
    db.add(log)
    db.commit()

    return {"success": True, "new_stock": new_qty}


@router.get("/admin/inventory/transactions")
def get_stock_history(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    logs = (
        db.query(StockLog)
        .join(Ingredient)
        .filter(StockLog.store_id == current_user.store_id)
        .order_by(desc(StockLog.created_at))
        .limit(limit)
        .all()
    )

    history = []
    for l in logs:
        history.append(
            {
                "date": l.created_at.strftime("%d/%m %H:%M"),
                "item": l.ingredient.name,
                "type": l.movement_type,
                "qty": l.quantity,
                "reason": l.reason,
                "user": l.user_name or "Sistema",
            }
        )
    return history


# 3. ROTA DE LISTAR CONTAS A PAGAR (FINANCEIRO)
@router.get("/admin/finance/bills")
def list_bills(
    status: str = Query("all"),  # pending, paid, all
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    query = db.query(Bill).filter(Bill.store_id == current_user.store_id)

    if status == "pending":
        query = query.filter(Bill.paid_at == None)
    elif status == "paid":
        query = query.filter(Bill.paid_at != None)

    bills = query.order_by(desc(Bill.due_date)).all()
    return bills  # Serializar no frontend ou usar template response


@router.post("/admin/finance/bills/save")
def save_bill(
    bill_id: Optional[int] = Form(None),
    description: str = Form(...),
    amount: float = Form(...),
    due_date: str = Form(...),
    payment_method: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    try:
        dt_due = datetime.strptime(due_date, "%Y-%m-%d")

        if bill_id:
            # Edição
            bill = db.query(Bill).get(bill_id)
            if not bill or bill.store_id != current_user.store_id:
                return JSONResponse(
                    status_code=403, content={"message": "Conta não encontrada."}
                )

            bill.description = description
            bill.amount = amount
            bill.due_date = dt_due
            bill.payment_method = payment_method
        else:
            # Criação Manual
            bill = Bill(
                store_id=current_user.store_id,
                description=description,
                amount=amount,
                due_date=dt_due,
                payment_method=payment_method,
                paid_at=None,  # Nasce pendente
            )
            db.add(bill)

        db.commit()
        return {"success": True, "message": "Conta salva com sucesso!"}

    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"message": str(e)})


@router.delete("/admin/finance/bills/{bill_id}")
def delete_bill(
    bill_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    bill = db.query(Bill).get(bill_id)
    if not bill or bill.store_id != current_user.store_id:
        return JSONResponse(
            status_code=403, content={"message": "Conta não encontrada."}
        )

    try:
        db.delete(bill)
        db.commit()
        return {"success": True}
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"message": str(e)})


@router.post("/admin/finance/bills/{bill_id}/pay")
def pay_bill(
    bill_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    bill = db.query(Bill).get(bill_id)
    if not bill or bill.store_id != current_user.store_id:
        return JSONResponse(
            status_code=403, content={"message": "Conta não encontrada."}
        )

    if bill.paid_at:
        return JSONResponse(
            status_code=400, content={"message": "Esta conta já está paga."}
        )

    bill.paid_at = datetime.now()
    db.commit()
    return {"success": True, "message": "Pagamento registrado!"}


@router.post("/admin/finance/bills/{bill_id}/reverse")
def reverse_bill(
    bill_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    bill = db.query(Bill).get(bill_id)
    if not bill or bill.store_id != current_user.store_id:
        return JSONResponse(
            status_code=403, content={"message": "Conta não encontrada."}
        )

    if not bill.paid_at:
        return JSONResponse(
            status_code=400, content={"message": "Esta conta ainda não foi paga."}
        )

    bill.paid_at = None  # Remove a data de pagamento
    db.commit()
    return {"success": True, "message": "Pagamento estornado (voltou para Pendente)."}
