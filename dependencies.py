from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials, APIKeyCookie
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import get_db
from models import User, Order, Store
from auth import verify_password, SECRET_KEY, ALGORITHM
from jose import JWTError, jwt
import pytz
from datetime import datetime

# Configurações globais que vamos centralizar aqui
security = HTTPBasic()
templates = Jinja2Templates(directory="templates")


def get_mixed_current_user(
    request: Request, 
    db: Session = Depends(get_db)
):
    """
    Tenta autenticar por Cookie (Apps) PRIMEIRO.
    Se falhar, tenta por Basic Auth (Admin).
    Isso evita que o App do Garçom peça senha de admin.
    """
    # 1. Tenta pelo Cookie (JWT) - Usado por Garçom e Motoboy
    token = request.cookies.get("access_token")
    if token:
        try:
            if "Bearer " in token: token = token.split(" ")[1]
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            email = payload.get("sub")
            user = db.query(User).filter(User.email == email).first()
            if user:
                return user
        except:
            pass # Se falhar o cookie, tenta o próximo método

    # 2. Se não tem cookie válido, tenta Basic Auth (Usado pelo Admin no PC)
    # Verificamos o header manualmente para não forçar o popup do navegador
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            import base64
            encoded = auth_header.split(" ")[1]
            decoded = base64.b64decode(encoded).decode("utf-8")
            username, password = decoded.split(":")
            
            user = db.query(User).filter(User.email == username).first()
            if user and verify_password(password, user.hashed_password):
                return user
        except:
            pass
            
    # 3. Se nada funcionar, lança erro 401 (Sem popup nativo chato)
    raise HTTPException(status_code=401, detail="Não autenticado")


# --- FILTROS JINJA ---
def format_brazil_time(dt):
    if not dt: return dt
    utc_zone = pytz.utc
    br_zone = pytz.timezone('America/Sao_Paulo')
    if dt.tzinfo is None: dt = utc_zone.localize(dt)
    return dt.astimezone(br_zone)

templates.env.filters["brazil_time"] = format_brazil_time


# --- AUTENTICAÇÃO VIA BANCO DE DADOS ---
def check_db_auth(credentials: HTTPBasicCredentials = Depends(security), db: Session = Depends(get_db)):
    """Verifica usuário/senha no banco de dados e retorna o Objeto User"""
    user = db.query(User).filter(User.email == credentials.username).first()
    
    if not user or not verify_password(credentials.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email ou senha incorretos",
            headers={"WWW-Authenticate": "Basic"},
        )
    return user


# --- VERIFICADOR DE PERMISSÕES ---
def check_role(allowed_roles: list):
    def role_checker(credentials: HTTPBasicCredentials = Depends(security), db: Session = Depends(get_db)):
        user = check_db_auth(credentials, db)
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Acesso negado. Requer nível: {', '.join(allowed_roles)}"
            )
        return user
    return role_checker

# --- FUNÇÕES AUXILIARES ---
def get_today_stats(db: Session, store_id: int):
    """Calcula caixa do dia para uma loja específica"""
    tz_br = pytz.timezone('America/Sao_Paulo')
    now_br = datetime.now(tz_br)
    today_start_br = now_br.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_br.astimezone(pytz.utc)

    stats = db.query(
        func.count(Order.id).label('count'),
        func.sum(Order.total_value).label('total')
    ).filter(
        Order.store_id == store_id,
        Order.created_at >= today_start_utc.replace(tzinfo=None)
    ).first()

    return {"count": stats.count or 0, "total": stats.total or 0.0}

# --- AUTENTICAÇÃO VIA COOKIE (PARA O APP MOTOBOY) ---
def get_current_driver(request: Request, db: Session = Depends(get_db)):
    # CORREÇÃO: Busca o cookie correto
    token = request.cookies.get("driver_token") 
    
    if not token:
        # Fallback: Tenta achar no header Authorization se o cookie falhar
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
            
    if not token:
        return None
    
    try:
        # Remove o prefixo "Bearer " se existir (comum em cookies mistos)
        if token.startswith("Bearer "): token = token.split(" ")[1]
        
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None: return None
    except JWTError:
        return None
    
    user = db.query(User).filter(User.email == email).first()
    if not user or user.role != 'driver':
        return None
        
    return user


# 1. AUTENTICAÇÃO DO GARÇOM (Reutiliza lógica segura)
def get_current_waiter(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("access_token")
    if not token: return None
    try:
        if token.startswith("Bearer "): token = token.split(" ")[1]
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None: return None
    except JWTError: return None
    
    # Permite Owner, Manager e Viewer (Caixa/Garçom)
    user = db.query(User).filter(User.email == email).first()
    if not user or user.role not in ['owner', 'manager', 'viewer', 'waiter']:
        return None
    return user