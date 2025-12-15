from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from enum import Enum

# Enum para alinhar com o banco
class TransactionTypeEnum(str, Enum):
    VALE = "vale"
    CONSUMO = "consumo"
    BONUS = "bonus"
    PAGAMENTO = "pagamento"

# --- Schema para PAGAR PEDIDO (PDV) ---
class PayByEmployeeRequest(BaseModel):
    order_id: int
    employee_id: int
    discount_percentage: float = 0.0  # Ex: 10.0 para 10%
    admin_password: Optional[str] = None # Opcional: senha extra de segurança

# --- Schema para LANÇAMENTO MANUAL (Gestão: Vale/Bônus) ---
class ManualTransactionCreate(BaseModel):
    employee_id: int
    amount: float
    transaction_type: TransactionTypeEnum # 'vale' ou 'bonus'
    description: str

# --- Schema para RESPOSTA DO EXTRATO (Visualização) ---
class TransactionResponse(BaseModel):
    id: int
    amount: float
    transaction_type: str
    description: Optional[str]
    created_at: datetime
    employee_name: str # Vamos enviar o nome já pronto para facilitar o front
    admin_name: str
    order_id: Optional[int]
    discount_percentage: Optional[float]

    class Config:
        from_attributes = True