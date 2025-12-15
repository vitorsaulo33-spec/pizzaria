# Arquivo: pizzaria/services/integrations/base.py
from abc import ABC, abstractmethod
from typing import List, Dict, Any
from datetime import datetime

class BaseIntegration(ABC):
    """
    Classe Abstrata que define o contrato para qualquer integração (Wabiz, iFood, Saipos, etc).
    """
    def __init__(self, token: str, user: str = None, password: str = None, base_url: str = None):
        self.token = token
        self.user = user
        self.password = password
        self.base_url = base_url

    @abstractmethod
    def fetch_orders(self) -> List[Dict[str, Any]]:
        """
        Deve ir na API externa e buscar os pedidos pendentes.
        Retorna: Lista de objetos brutos (como vieram da API).
        """
        pass

    @abstractmethod
    def normalize_order(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Converte o JSON bagunçado da API externa para o PADRÃO PURO do ALIV.
        
        Deve retornar EXATAMENTE este formato:
        {
            "external_id": str,
            "total_value": float,
            "created_at": datetime,
            "customer": {
                "name": str,
                "phone": str, # Já formatado com 55...
                "email": str
            },
            "address": {
                "street": str,
                "number": str,
                "neighborhood": str,
                "city": str,
                "state": str,
                "zip_code": str
            },
            "payment_method": str,
            "items": [
                {"title": str, "quantity": float, "price": float}
            ],
            "raw_source": dict # O JSON original para debug
        }
        """
        pass