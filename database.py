from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# --- CONFIGURAÇÃO ROBUSTA DE CONEXÃO ---
engine = create_engine(
    DATABASE_URL,  
    pool_size=10,       # Reduzido para evitar erro de "Too many connections"
    max_overflow=20,    # Margem de segurança mais conservadora
    pool_timeout=30,
    pool_recycle=1800
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# Dependência para usar nas rotas
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()