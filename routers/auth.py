from fastapi import APIRouter, Request, Depends, Form, Query
from sqlalchemy.orm import Session
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from datetime import timedelta
from typing import Optional

# Imports Locais
from database import get_db
from models import User, Store, Order
from auth import verify_password, get_password_hash, create_access_token, SECRET_KEY, ALGORITHM
from dependencies import templates, check_db_auth, check_role, get_today_stats

router = APIRouter()


@router.get("/logout")
def logout(
    request: Request, 
    target: str = Query("admin") # <--- Aceita ?target=driver ou ?target=waiter
):
    # Define para onde vai
    url = "/admin/dashboard" # Padrão
    
    if target == "driver":
        url = "/driver/login"
    elif target == "waiter":
        url = "/waiter/login"
        
    response = RedirectResponse(url=url, status_code=303)
    response.delete_cookie("access_token")
    return response


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_view(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role(["owner"])),
    search: Optional[str] = None,
):
    query = db.query(User)

    if search:
        term = f"%{search}%"
        query = query.filter((User.full_name.ilike(term)) | (User.email.ilike(term)))

    users = query.order_by(User.full_name).all()
    stores = db.query(Store).all()
    today_stats = get_today_stats(db, current_user.store_id)

    # O Template correto é users.html
    return templates.TemplateResponse(
        "users.html",
        {
            "request": request,
            "users": users,
            "stores": stores,
            "today_stats": today_stats,
            "filters": {"search": search or ""},
            "current_user": current_user,
        },
    )


# --- API: CRIAR / EDITAR USUÁRIO ---
@router.post("/admin/users/save")
async def save_user(
    user_id: Optional[str] = Form(None),
    full_name: str = Form(...),
    email: str = Form(...),
    phone: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    store_id: str = Form(...),
    role: str = Form(...),
    driver_fixed_fee: float = Form(0.0), # <--- NOVO CAMPO
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role(["owner"])),
):
    try:
        final_user_id = int(user_id) if user_id and user_id.strip() else None
        final_store_id = int(store_id) if store_id and store_id.strip() else None

        if not final_user_id:
            # Criação
            exists = db.query(User).filter(User.email == email).first()
            if exists: return JSONResponse(status_code=400, content={"message": "Email já existe."})
            if not password: return JSONResponse(status_code=400, content={"message": "Senha obrigatória."})

            new_user = User(
                full_name=full_name, email=email, phone=phone, hashed_password=get_password_hash(password),
                store_id=final_store_id, role=role, driver_fixed_fee=driver_fixed_fee
            )
            db.add(new_user)
        else:
            # Edição
            user = db.query(User).get(final_user_id)
            if not user: return JSONResponse(status_code=404, content={"message": "Usuário não encontrado."})

            user.full_name = full_name
            user.email = email
            user.phone = phone
            user.store_id = final_store_id
            user.role = role
            user.driver_fixed_fee = driver_fixed_fee # <--- Salva a taxa

            if password and password.strip():
                user.hashed_password = get_password_hash(password)

        db.commit()
        return {"success": True}

    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"message": str(e)})


# --- API: EXCLUIR USUÁRIO ---
@router.delete("/admin/users/{user_id}")
async def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    if user_id == current_user.id:
        return JSONResponse(
            status_code=400, content={"message": "Você não pode se excluir."}
        )

    user = db.query(User).get(user_id)
    if not user:
        return JSONResponse(
            status_code=404, content={"message": "Usuário não encontrado"}
        )

    try:
        db.delete(user)
        db.commit()
        return {"success": True}
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"message": "Erro ao excluir. Verifique dependências."},
        )


# ==========================================
#           APP DO MOTOBOY (PWA)
# ==========================================


# 1. TELA DE LOGIN (GET)
@router.get("/driver/login", response_class=HTMLResponse)
def driver_login_page(request: Request):
    return templates.TemplateResponse("driver_login.html", {"request": request})


@router.post("/driver/login", response_class=HTMLResponse)
async def driver_login_action(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    # print(f"🔑 [Auth] Tentativa de login motorista: {email}") <--- REMOVER
    
    user = db.query(User).filter(User.email == email).first()

    # --- INÍCIO DA CORREÇÃO (Adicionando a validação que faltava) ---
    if not user or not verify_password(password, user.hashed_password) or user.role != "driver":
        return templates.TemplateResponse(
            "driver_login.html", 
            {"request": request, "error": "Credenciais inválidas ou acesso negado."}
        )
    # --- FIM DA CORREÇÃO ---

    # Sucesso: Cria Token
    access_token = create_access_token(
        data={"sub": user.email}, expires_delta=timedelta(hours=12)
    )

    response = RedirectResponse(url="/driver/app", status_code=303)
    
    response.set_cookie(
        key="driver_token",
        value=access_token,
        httponly=True,
        max_age=43200,
        path="/",
        samesite="lax",
        secure=False
    )
    
    return response


# 2. TELA DE LOGIN
@router.get("/waiter/login", response_class=HTMLResponse)
def waiter_login_page(request: Request):
    # CORREÇÃO: Usando o novo template waiter_login.html
    return templates.TemplateResponse("waiter_login.html", {"request": request})


@router.post("/waiter/login", response_class=HTMLResponse)
async def waiter_login_action(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email).first()
    if (
        not user
        or not verify_password(password, user.hashed_password)
        or user.role not in ["owner", "manager", "viewer", "waiter"]
    ):
        # CORREÇÃO: Chama a tela do garçom novamente
        return templates.TemplateResponse(
            "waiter_login.html", {"request": request, "error": "Acesso negado!"}
        )

    access_token = create_access_token(
        data={"sub": user.email}, expires_delta=timedelta(hours=12)
    )
    response = RedirectResponse(url="/waiter/app", status_code=303)
    response.set_cookie(
        key="access_token", value=f"Bearer {access_token}", httponly=True, max_age=43200
    )
    return response


# --- ROTA DE SEGURANÇA: VALIDAR SENHA DE ADM/GERENTE ---
@router.post("/admin/auth/verify-admin")
async def verify_admin_action(
    password: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(check_db_auth),
):
    """
    Verifica se a senha fornecida pertence a algum Dono ou Gerente da loja atual.
    """
    admins = (
        db.query(User)
        .filter(
            User.store_id == current_user.store_id, User.role.in_(["owner", "manager"])
        )
        .all()
    )

    for admin in admins:
        if verify_password(password, admin.hashed_password):
            return {"success": True, "authorized_by": admin.full_name}

    # AQUI ESTÁ A CORREÇÃO: MUDAMOS DE 401 PARA 403
    return JSONResponse(
        status_code=403, content={"message": "Senha inválida ou sem permissão."}
    )

# --- ADICIONE ISTO NO FINAL DE pizzaria/routers/auth.py ---

@router.get("/admin/api/employees/list")
def get_all_employees(
    db: Session = Depends(get_db), 
    current_user: User = Depends(check_db_auth)
):
    """
    Retorna lista unificada de todos os usuários (Motoristas, Garçons, Cozinha, Gerentes)
    Para popular selects de lançamento financeiro.
    """
    employees = db.query(User).filter(
        User.store_id == current_user.store_id,
        User.is_active == True  # Garanta que User tenha is_active ou remova essa linha se não tiver
    ).all()
    
    result = []
    for emp in employees:
        # Traduz o role para exibir bonito
        role_map = {
            "owner": "Dono",
            "manager": "Gerente",
            "driver": "Motoboy",
            "waiter": "Garçom",
            "kitchen": "Cozinha"
        }
        role_name = role_map.get(emp.role, emp.role.capitalize())
        
        result.append({
            "id": emp.id,
            "name": f"{emp.full_name} ({role_name})", # Ex: Vitor (Dono)
            "role": emp.role
        })
        
    return result