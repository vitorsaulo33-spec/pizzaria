from typing import List, Dict
from fastapi import WebSocket

class ConnectionManager:
    def __init__(self):
        # Dicionário: { store_id: [lista_de_sockets_conectados] }
        self.active_connections: Dict[int, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, store_id: int):
        await websocket.accept()
        if store_id not in self.active_connections:
            self.active_connections[store_id] = []
        self.active_connections[store_id].append(websocket)
        print(f"🔌 [Socket] KDS conectado na Loja {store_id}")

    def disconnect(self, websocket: WebSocket, store_id: int):
        if store_id in self.active_connections:
            if websocket in self.active_connections[store_id]:
                self.active_connections[store_id].remove(websocket)
                print(f"🔌 [Socket] KDS desconectado da Loja {store_id}")

    async def broadcast(self, store_id: int, message: str):
        """Envia mensagem para TODOS os KDS dessa loja"""
        if store_id in self.active_connections:
            # Copia a lista para evitar erro de modificação durante iteração
            connections = self.active_connections[store_id][:]
            for connection in connections:
                try:
                    await connection.send_text(message)
                except Exception as e:
                    # Se der erro (ex: fechou navegador), remove da lista
                    print(f"⚠️ Erro ao enviar socket: {e}")
                    self.disconnect(connection, store_id)

# Instância Global (Singleton)
manager = ConnectionManager()