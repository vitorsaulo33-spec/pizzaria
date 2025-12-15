# Arquivo: pizzaria/celery_app.py
import os
from celery import Celery
from dotenv import load_dotenv

load_dotenv()

# Configura o Broker (Redis)
# Se estiver rodando local, geralmente é redis://localhost:6379/0
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "pizzaria_worker",
    broker=REDIS_URL,
    backend=REDIS_URL
)

# Configurações Adicionais
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="America/Sao_Paulo",
    enable_utc=True,
)

# Onde o Celery vai procurar as tarefas (tasks)
celery_app.autodiscover_tasks(['services.tasks'])