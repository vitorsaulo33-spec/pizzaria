from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
import bcrypt  # <--- Biblioteca nativa
import os

# Segredo para assinar os tokens
SECRET_KEY = os.getenv("SECRET_KEY")

if not SECRET_KEY:
    print("\n" + "="*50)
    print("🚨 PERIGO: SECRET_KEY NÃO ENCONTRADA NO .ENV")
    print("   Usando chave insegura de desenvolvimento.")
    print("="*50 + "\n")
    SECRET_KEY = "DEV_KEY_TEMPORARIA_NAO_USAR_EM_PROD"
    
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 horas

def verify_password(plain_password, hashed_password):
    """Verifica se a senha bate com o hash salvo"""
    # 1. Garante que a senha digitada seja bytes
    if isinstance(plain_password, str):
        plain_password = plain_password.encode('utf-8')
    
    # 2. Garante que o hash do banco seja bytes
    if isinstance(hashed_password, str):
        hashed_password = hashed_password.encode('utf-8')
    
    # 3. Verifica usando bcrypt nativo (NÃO usa passlib)
    return bcrypt.checkpw(plain_password, hashed_password)

def get_password_hash(password):
    """Gera um hash seguro da senha"""
    if isinstance(password, str):
        password = password.encode('utf-8')
    
    # Gera o salt e o hash
    hashed = bcrypt.hashpw(password, bcrypt.gensalt())
    return hashed.decode('utf-8') # Retorna como string para salvar no banco

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt