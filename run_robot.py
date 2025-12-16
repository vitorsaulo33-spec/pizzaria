import time
import pytz
import os
from apscheduler.schedulers.background import BackgroundScheduler
from database import SessionLocal

# --- Imports dos Jobs (Iguais ao main.py) ---
from services.background_jobs import (
    sync_external_orders,
    run_opportunity_scanner,
    send_morning_reports,
    run_rfm_analysis_cron,
    dispatch_smart_event
)
from services.crm_engine import run_crm_automations

def rodar_robo():
    print("🤖 [Robô Dedicado] Iniciando processo único...")

    # Garante fuso horário do Brasil
    br_timezone = pytz.timezone("America/Sao_Paulo")
    scheduler = BackgroundScheduler(timezone=br_timezone)

    # --- Configuração dos Jobs (Copiado do seu main.py original) ---
    
    # 1. Sincronizar pedidos externos (Wabiz/iFood) a cada 30 segundos
    scheduler.add_job(sync_external_orders, "interval", seconds=30)
    
    # 2. Automações de CRM (Mensagens automáticas) - roda a cada hora cheia
    # Nota: Instanciamos o banco aqui
    scheduler.add_job(run_crm_automations, "cron", minute=0, args=[SessionLocal()])
    
    # 3. Scanner de Oportunidades (Recuperação de vendas) - a cada 60 min
    scheduler.add_job(run_opportunity_scanner, "interval", minutes=60)
    
    # 4. Relatórios Matinais (08:00 da manhã)
    scheduler.add_job(send_morning_reports, "cron", hour=8, minute=0)
    
    # 5. Análise RFM (Classificação de clientes) - às 22:35
    scheduler.add_job(run_rfm_analysis_cron, "cron", hour=22, minute=35)

    # Inicia o agendador
    scheduler.start()
    print("✅ [Robô Dedicado] Todos os agendamentos ativos. Pressione Ctrl+C para sair.")

    # Loop infinito para manter o script rodando (Isso é crucial para o Systemd)
    try:
        while True:
            time.sleep(2)
    except (KeyboardInterrupt, SystemExit):
        print("🛑 Parando robô...")
        scheduler.shutdown()

if __name__ == "__main__":
    rodar_robo()