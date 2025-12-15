from database import engine
from sqlalchemy import text

def create_indexes():
    print("🚀 Otimizando Banco de Dados (Modo Blindado)...")
    
    # Lista de índices vitais com comando "IF NOT EXISTS" para não dar erro
    commands = [
        "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);",
        "CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders (created_at);",
        "CREATE INDEX IF NOT EXISTS idx_orders_store_status ON orders (store_id, status);",
        "CREATE INDEX IF NOT EXISTS idx_orders_customer_phone ON orders (customer_phone);"
    ]

    with engine.connect() as conn:
        # Habilita o autocommit para que cada comando seja independente
        # Isso impede que um erro trave a transação inteira
        conn.execution_options(isolation_level="AUTOCOMMIT")
        
        for sql in commands:
            try:
                print(f"🔧 Executando: {sql}")
                conn.execute(text(sql))
                print("   ✅ Sucesso.")
            except Exception as e:
                # Se der erro, mostra mas continua para o próximo
                print(f"   ⚠️ Aviso (Pode ignorar se já existir): {e}")

    print("🏁 Otimização concluída! O sistema deve voar agora.")

if __name__ == "__main__":
    create_indexes()