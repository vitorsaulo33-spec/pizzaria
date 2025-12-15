from sqlalchemy import Column, Integer, String, Text, DateTime, func, Boolean, Float, ForeignKey, UniqueConstraint, Enum as SqlEnum
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB
from database import Base
from datetime import datetime, date
import enum

# ==========================================
#          AUTENTICAÇÃO E LOJAS
# ==========================================

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    phone = Column(String, nullable=True)
    hashed_password = Column(String)
    full_name = Column(String)
    store_id = Column(Integer, ForeignKey("stores.id"))
    created_at = Column(DateTime, server_default=func.now())
    role = Column(String, default="owner")
    
    # --- NOVOS CAMPOS MOTOBOY ---
    driver_fixed_fee = Column(Float, default=0.0) # Taxa fixa/Diária
    driver_balance = Column(Float, default=0.0)   # Saldo Devedor/Crédito acumulado
    
    store = relationship("Store", back_populates="users")
    
# --- NOVA TABELA: TRANSAÇÕES DE CAIXA (SANGRIA/SUPLEMENTO) ---
class CashTransaction(Base):
    __tablename__ = "cash_transactions"
    
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    user_id = Column(Integer, ForeignKey("users.id")) # Quem fez
    
    type = Column(String) # 'SANGRIA', 'SUPLEMENTO', 'PAGAMENTO_MOTOBOY'
    amount = Column(Float)
    description = Column(String)
    
    created_at = Column(DateTime, server_default=func.now())
    
    store = relationship("Store")
    
    # === NOVO CAMPO: Vínculo com a Sessão do Caixa ===
    cash_opening_id = Column(Integer, ForeignKey("cash_openings.id"), nullable=True)
    # =================================================
    

class Store(Base):
    __tablename__ = "stores"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    slug = Column(String, unique=True, index=True)
    
    # Configurações de API
    fb_pixel_id = Column(String, nullable=True)
    fb_access_token = Column(String, nullable=True)
    ga4_measurement_id = Column(String, nullable=True)
    ga4_api_secret = Column(String, nullable=True)
    
    # Configurações WhatsApp
    whatsapp_number = Column(String, nullable=True)
    whatsapp_api_token = Column(String, nullable=True) 
    whatsapp_phone_id = Column(String, nullable=True)
    
    # CRM & Operação
    crm_schedule_hour = Column(Integer, default=18)
    is_open = Column(Boolean, default=True)
    
    # Localização
    address_lat = Column(String, nullable=True)
    address_lng = Column(String, nullable=True)
    address_name = Column(String, nullable=True)
    address_text = Column(String, nullable=True)
    
    # Integração (Hub)
    integration_type = Column(String, default="wabiz")
    integration_url = Column(String, nullable=True)
    integration_user = Column(String, nullable=True)
    integration_password = Column(String, nullable=True)
    
    created_at = Column(DateTime, server_default=func.now())

    # Relacionamentos
    users = relationship("User", back_populates="store")
    orders = relationship("Order", back_populates="store")
    events = relationship("Event", back_populates="store")
    categories = relationship("Category", back_populates="store")
    campaigns = relationship("Campaign", back_populates="store")
    insights = relationship("Insight", back_populates="store")
    
    ingredients = relationship("Ingredient", back_populates="store")
    pizza_sizes = relationship("PizzaSize", back_populates="store")
    addons = relationship("ProductAddon", back_populates="store")
    sectors = relationship("ProductionSector", back_populates="store")

    # CAMPO ÚNICO E PODEROSO
    integrations_config = Column(JSONB, default={})
# ==========================================
#        CATÁLOGO E PRODUTOS (CORE)
# ==========================================

class ProductionSector(Base):
    __tablename__ = "production_sectors"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    name = Column(String) 
    printer_ip = Column(String, nullable=True) 
    has_expedition = Column(Boolean, default=True)
    store = relationship("Store", back_populates="sectors")
    products = relationship("Product", back_populates="sector")

class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    name = Column(String)
    order_index = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    
    # --- NOVO CAMPO ---
    sector_id = Column(Integer, ForeignKey("production_sectors.id"), nullable=True)
    # ------------------
    
    store = relationship("Store", back_populates="categories")
    products = relationship("Product", back_populates="category")
    sector = relationship("ProductionSector")

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    category_id = Column(Integer, ForeignKey("categories.id"))
    sector_id = Column(Integer, ForeignKey("production_sectors.id"), nullable=True)
    
    name = Column(String)
    description = Column(Text)
    price = Column(Float)
    image_url = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    
    # Configurações de Pizza
    is_pizza = Column(Boolean, default=False) 
    allows_flavors = Column(Boolean, default=False)
    max_flavors = Column(Integer, default=1)
    base_type = Column(String, default="salgada") 
    
    # --- NOVOS CAMPOS (V8) ---
    config = Column(JSONB, default={}) # Ex: {allowed_addons: [1, 2]}
    combo_items = Column(JSONB, default=[]) # Ex: [{product_id: 10, qty: 2}]
    
    # Relacionamentos
    category = relationship("Category", back_populates="products")
    sector = relationship("ProductionSector", back_populates="products")
    recipe_items = relationship("ProductRecipe", back_populates="product")
    mappings = relationship("ProductMapping", back_populates="product")
    size_prices = relationship("ProductSizePrice", back_populates="product", cascade="all, delete-orphan")
    
    preparation_method = Column(Text, nullable=True)

class ProductSizePrice(Base):
    __tablename__ = "product_size_prices"
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"))
    size_id = Column(Integer, ForeignKey("pizza_sizes.id"))
    
    price = Column(Float, default=0.0)
    cost_price = Column(Float, default=0.0)
    is_active = Column(Boolean, default=True)
    
    product = relationship("Product", back_populates="size_prices")
    size = relationship("PizzaSize")

# ==========================================
#      TAMANHOS, BORDAS E ADICIONAIS
# ==========================================

class PizzaSize(Base):
    __tablename__ = "pizza_sizes"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    
    name = Column(String)
    slug = Column(String)
    slices = Column(Integer, default=8)
    recipe_multiplier = Column(Float, default=1.0) 
    
    store = relationship("Store", back_populates="pizza_sizes")
    addon_prices = relationship("AddonPrice", back_populates="size")
    product_recipes = relationship("ProductRecipe", back_populates="size")
    base_recipes = relationship("PizzaBaseRecipe", back_populates="size")
    
    # --- NOVO CAMPO ---
    max_flavors = Column(Integer, default=1) # Qtd de sabores permitidos neste tamanho

class ProductAddon(Base):
    __tablename__ = "product_addons"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    
    name = Column(String) 
    addon_type = Column(String) # "edge", "extra"
    is_active = Column(Boolean, default=True)
    
    # --- NOVO CAMPO (V9) ---
    valid_categories = Column(JSONB, default=[]) # Ex: [1, 5] (Só aparece em Pizzas)
    
    store = relationship("Store", back_populates="addons")
    prices = relationship("AddonPrice", back_populates="addon")

class AddonPrice(Base):
    __tablename__ = "addon_prices"
    id = Column(Integer, primary_key=True, index=True)
    addon_id = Column(Integer, ForeignKey("product_addons.id"))
    size_id = Column(Integer, ForeignKey("pizza_sizes.id"), nullable=True)
    
    price = Column(Float, default=0.0)
    
    # --- NOVO CAMPO (V8) ---
    external_code = Column(String, nullable=True) # Código de integração (Ex: ifood_borda_g)
    
    addon = relationship("ProductAddon", back_populates="prices")
    size = relationship("PizzaSize", back_populates="addon_prices")
    recipe_items = relationship("AddonRecipe", back_populates="addon_price") 

# ==========================================
#      ESTOQUE E FICHA TÉCNICA
# ==========================================

class InventoryCategory(Base):
    __tablename__ = "inventory_categories"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    name = Column(String)
    
    store = relationship("Store")
    ingredients = relationship("Ingredient", back_populates="category_rel")

class InventoryUnit(Base):
    __tablename__ = "inventory_units"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    name = Column(String)
    
    store = relationship("Store")
    ingredients = relationship("Ingredient", back_populates="unit_rel", foreign_keys="[Ingredient.unit_id]")

class Ingredient(Base):
    __tablename__ = "ingredients"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    
    name = Column(String)
    category_id = Column(Integer, ForeignKey("inventory_categories.id"), nullable=True)
    
    # Unidades
    unit_id = Column(Integer, ForeignKey("inventory_units.id"), nullable=True) # Estoque
    input_unit_id = Column(Integer, ForeignKey("inventory_units.id"), nullable=True) # Compra
    usage_unit_id = Column(Integer, ForeignKey("inventory_units.id"), nullable=True) # Baixa
    
    current_stock = Column(Float, default=0.0)
    min_stock = Column(Float, default=0.0)
    max_stock = Column(Float, default=0.0)
    cost = Column(Float, default=0.0)
    
    # Validade (V10)
    expiration_date = Column(DateTime, nullable=True)
    
    integration_code = Column(String, index=True, nullable=True)
    is_available_for_sale = Column(Boolean, default=False) 
    conversion_factor = Column(Float, default=1.0) 
    
    category_legacy = Column("category", String, nullable=True) 
    unit_legacy = Column("unit", String, nullable=True)

    store = relationship("Store", back_populates="ingredients")
    category_rel = relationship("InventoryCategory", back_populates="ingredients")
    
    # Relacionamentos de Unidade
    unit_rel = relationship("InventoryUnit", foreign_keys=[unit_id], back_populates="ingredients")
    input_unit_rel = relationship("InventoryUnit", foreign_keys=[input_unit_id])
    usage_unit_rel = relationship("InventoryUnit", foreign_keys=[usage_unit_id])
    
    product_usages = relationship("ProductRecipe", back_populates="ingredient")
    base_usages = relationship("PizzaBaseRecipe", back_populates="ingredient")
    addon_usages = relationship("AddonRecipe", back_populates="ingredient")

    # --- RELACIONAMENTO DE PRODUÇÃO (CORRIGIDO) ---
    production_recipe = relationship(
        "IngredientRecipe", 
        back_populates="parent_ingredient", 
        cascade="all, delete-orphan",
        foreign_keys="[IngredientRecipe.parent_ingredient_id]"
    )

# --- NOVA TABELA: LOG DE MOVIMENTAÇÃO (KARDEX) ---
class StockLog(Base):
    __tablename__ = "stock_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"))
    
    user_name = Column(String) # Quem fez
    movement_type = Column(String) # 'IN', 'OUT', 'ADJUST'
    quantity = Column(Float) # Quanto movimentou
    cost_at_time = Column(Float) # Custo no momento
    old_stock = Column(Float)
    new_stock = Column(Float)
    reason = Column(String) # Ex: "Compra", "Validade", "Quebra", "Contagem"
    
    created_at = Column(DateTime, server_default=func.now())
    
    store = relationship("Store")
    ingredient = relationship("Ingredient")

class PizzaBaseRecipe(Base):
    __tablename__ = "pizza_base_recipes"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    size_slug = Column(String, nullable=True) 
    size_id = Column(Integer, ForeignKey("pizza_sizes.id"), nullable=True)
    base_type = Column(String, default="salgada")
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"))
    quantity = Column(Float)
    
    ingredient = relationship("Ingredient", back_populates="base_usages")
    size = relationship("PizzaSize", back_populates="base_recipes")

class ProductRecipe(Base):
    __tablename__ = "product_recipes"
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"))
    size_id = Column(Integer, ForeignKey("pizza_sizes.id"), nullable=True) 
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"))
    quantity = Column(Float)
    
    product = relationship("Product", back_populates="recipe_items")
    ingredient = relationship("Ingredient", back_populates="product_usages")
    size = relationship("PizzaSize", back_populates="product_recipes")

class AddonRecipe(Base):
    __tablename__ = "addon_recipes"
    id = Column(Integer, primary_key=True, index=True)
    addon_price_id = Column(Integer, ForeignKey("addon_prices.id"))
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"))
    quantity = Column(Float)
    
    addon_price = relationship("AddonPrice", back_populates="recipe_items")
    ingredient = relationship("Ingredient", back_populates="addon_usages")

# ==========================================
#           PEDIDOS E EVENTOS
# ==========================================

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    wabiz_id = Column(String, index=True, nullable=True)
    external_id = Column(String, index=True, nullable=True)
    
    customer_name = Column(String)
    customer_phone = Column(String)
    customer_email = Column(String)
    
    address_street = Column(String)
    address_number = Column(String)
    address_neighborhood = Column(String)
    address_city = Column(String)
    address_state = Column(String)
    address_complement = Column(String, nullable=True)
    
    total_value = Column(Float)
    payment_method = Column(String)
    items_json = Column(JSONB)
    
    sent_to_facebook = Column(Boolean, default=False)
    sent_to_google = Column(Boolean, default=False)
    sent_thank_you_msg = Column(Boolean, default=False)
    sent_nps = Column(Boolean, default=False)
    
    created_at = Column(DateTime, server_default=func.now())
    
    # --- NOVOS CAMPOS PARA O MOTOBOY ---
    driver_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    status = Column(String, default="PREPARO") # PENDENTE, PREPARO, SAIU_ENTREGA, ENTREGUE
    
    # Relacionamentos
    store = relationship("Store", back_populates="orders")
    driver = relationship("User", foreign_keys=[driver_id]) # <--- Vínculo com o Usuário Motorista
    
    # --- NOVOS CAMPOS (ADICIONE ESTES) ---
    table_number = Column(Integer, nullable=True)  # Mesa
    service_fee = Column(Float, default=0.0)       # Taxa de Serviço (10%)
    discount = Column(Float, default=0.0)          # Desconto
    
    
    # --- NOVOS CAMPOS PARA FECHAMENTO ---
    driver_tip = Column(Float, default=0.0)      # Caixinha do Motoboy
    customer_credit = Column(Float, default=0.0) # Crédito gerado para o cliente
    
    # Campo delivery_fee já existe? Se não, adicione:
    delivery_fee = Column(Float, default=0.0)
    delivery_type = Column(String, default="delivery")
    is_driver_paid = Column(Boolean, default=False)
    
    kds_timer_start = Column(DateTime, nullable=True)
    
    # --- NOVO CAMPO: Vínculo com Cliente ---
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    
    # observações do pedido
    notes = Column(Text, nullable=True)
    
    # --- Relacionamentos ---
    # Isso permite fazer order.customer.name no código
    customer = relationship("Customer", back_populates="orders")
    
    
    # Isso grava exatamente em qual abertura de caixa esse pedido nasceu
    cash_opening_id = Column(Integer, ForeignKey("cash_openings.id"), nullable=True)

class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id")) 
    event_name = Column(String, index=True)
    event_id = Column(String, index=True)
    url = Column(Text)
    user_agent = Column(Text)
    client_ip = Column(String)
    user_data = Column(JSONB)
    custom_data = Column(JSONB)
    sent_to_facebook = Column(Boolean, default=False)
    sent_to_google = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())
    store = relationship("Store", back_populates="events")

class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    phone = Column(String, index=True)
    name = Column(String)
    email = Column(String)
    birth_date = Column(DateTime, nullable=True) 
    total_spent = Column(Float, default=0) 
    order_count = Column(Integer, default=0)
    last_order_at = Column(DateTime)
    rfm_segment = Column(String, default="Novato") 
    rfm_score = Column(String) 
    created_at = Column(DateTime, server_default=func.now())
    store = relationship("Store")
    addresses = relationship("Address", back_populates="customer")
    # Relacionamento inverso (opcional, mas recomendado)
    orders = relationship("Order", back_populates="customer")

class Address(Base):
    __tablename__ = "addresses"
    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"))
    store_id = Column(Integer, ForeignKey("stores.id"))
    street = Column(String)
    number = Column(String)
    neighborhood = Column(String, index=True)
    city = Column(String)
    state = Column(String)
    zip_code = Column(String)
    complement = Column(String)
    created_at = Column(DateTime, server_default=func.now())
    last_used_at = Column(DateTime, default=datetime.now)
    customer = relationship("Customer", back_populates="addresses")
    store = relationship("Store")

class Campaign(Base):
    __tablename__ = "campaigns"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    name = Column(String) 
    trigger_type = Column(String) 
    days_delay = Column(Integer, default=0) 
    message_template = Column(Text) 
    meta_template_name = Column(String)
    is_active = Column(Boolean, default=True)
    scheduled_at = Column(DateTime, nullable=True)
    filter_rules = Column(JSONB)
    store = relationship("Store", back_populates="campaigns")
    logs = relationship("CampaignLog", back_populates="campaign")

class CampaignLog(Base):
    __tablename__ = "campaign_logs"
    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"))
    customer_phone = Column(String, index=True)
    customer_name = Column(String)
    sent_at = Column(DateTime, server_default=func.now())
    status = Column(String)
    message_id = Column(String)
    campaign = relationship("Campaign", back_populates="logs")

class Insight(Base):
    __tablename__ = "insights"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    type = Column(String)
    title = Column(String)
    message = Column(Text)
    action_prompt = Column(Text)
    is_archived = Column(Boolean, default=False)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())
    store = relationship("Store", back_populates="insights")

class ProductMapping(Base):
    __tablename__ = "product_mappings"
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"))
    store_id = Column(Integer, ForeignKey("stores.id"))
    integration_type = Column(String) 
    external_code = Column(String, index=True) 
    product = relationship("Product", back_populates="mappings")
    
    # --- SEGURANÇA DE BANCO ---
    # Garante que não exista o mesmo código externo para a mesma integração na mesma loja
    __table_args__ = (
        UniqueConstraint('store_id', 'integration_type', 'external_code', name='uix_store_integration_code'),
    )
    
class ImportedInvoice(Base):
    __tablename__ = "imported_invoices"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    access_key = Column(String, index=True) 
    imported_at = Column(DateTime, server_default=func.now())
    store = relationship("Store")
    
class Bill(Base):
    __tablename__ = "bills"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    
    description = Column(String)
    amount = Column(Float)
    due_date = Column(DateTime)
    paid_at = Column(DateTime, nullable=True)
    payment_method = Column(String)
    invoice_key = Column(String, nullable=True)
    
    created_at = Column(DateTime, server_default=func.now())
    store = relationship("Store")

class IngredientRecipe(Base):
    __tablename__ = "ingredient_recipes"
    id = Column(Integer, primary_key=True, index=True)
    parent_ingredient_id = Column(Integer, ForeignKey("ingredients.id"))
    child_ingredient_id = Column(Integer, ForeignKey("ingredients.id"))
    quantity = Column(Float)
    
    parent_ingredient = relationship("Ingredient", foreign_keys=[parent_ingredient_id], back_populates="production_recipe")
    child_ingredient = relationship("Ingredient", foreign_keys=[child_ingredient_id])
    
class DeliveryFee(Base):
    __tablename__ = "delivery_fees"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    neighborhood = Column(String)
    fee = Column(Float, default=0.0)
    
    store = relationship("Store")
    
class DriverSession(Base):
    __tablename__ = "driver_sessions"
    id = Column(Integer, primary_key=True, index=True)
    driver_id = Column(Integer, ForeignKey("users.id"))
    store_id = Column(Integer, ForeignKey("stores.id"))
    
    start_time = Column(DateTime, server_default=func.now())
    end_time = Column(DateTime, nullable=True)
    
    # Resumo fechado
    total_deliveries = Column(Integer, default=0)
    total_amount_due = Column(Float, default=0.0) # Valor que a casa deve pagar ao motoboy (Soma das Taxas)
    
    driver = relationship("User")
    advances = relationship("DriverAdvance", back_populates="session")
    
    # === NOVO CAMPO: Vínculo com o Caixa ===
    cash_opening_id = Column(Integer, ForeignKey("cash_openings.id"), nullable=True)
    # =======================================
    
class DriverAdvance(Base):
    """Vales / Adiantamentos para Motoboys"""
    __tablename__ = "driver_advances"
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("driver_sessions.id"))
    amount = Column(Float, default=0.0)
    reason = Column(String, nullable=True)
    is_paid = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())
    
    session = relationship("DriverSession", back_populates="advances")
    

class CashClosing(Base):
    """Histórico de Fechamento de Caixa (Z-Report)"""
    __tablename__ = "cash_closings"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    user_id = Column(Integer, ForeignKey("users.id")) # Quem fechou
    
    opened_at = Column(DateTime) # Início do período apurado
    closed_at = Column(DateTime, server_default=func.now())
    
    total_system = Column(Float) # Quanto o sistema diz que tem
    total_real = Column(Float)   # Quanto o usuário contou
    difference = Column(Float)   # Diferença (Quebra)
    
    breakdown_json = Column(JSONB) # Detalhe por método (Pix: 100, Dinheiro: 50...)
    notes = Column(Text, nullable=True)
    
    closer_name = Column(String, nullable=True) # Nome digitado na hora
    
    # === NOVO CAMPO ===
    next_opening_amount = Column(Float, default=0.0) # Valor deixado para o próximo caixa
    
class CashOpening(Base):
    """Registro de Abertura de Caixa (Fundo de Troco)"""
    __tablename__ = "cash_openings"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    amount = Column(Float, default=0.0)
    created_at = Column(DateTime, server_default=func.now())
    
    
# Definindo os tipos de transação para garantir consistência
class TransactionType(str, enum.Enum):
    # Mudamos para minúsculo para bater com o PostgreSQL ('vale', 'consumo'...)
    vale = "vale"
    consumo = "consumo"
    bonus = "bonus"
    pagamento = "pagamento"

class EmployeeTransaction(Base):
    __tablename__ = "employee_transactions"

    id = Column(Integer, primary_key=True, index=True)
    
    # Relacionamentos
    employee_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    admin_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)
    
    # Dados Financeiros
    amount = Column(Float, nullable=False)
    # A coluna usa o Enum acima
    transaction_type = Column(SqlEnum(TransactionType, name='transaction_type_enum'), nullable=False)
    
    # Detalhes
    discount_percentage = Column(Float, default=0.0)
    description = Column(String(255), nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    
#Tabela de eventos

class PendingPixelEvent(Base):
    __tablename__ = "pending_pixel_events"
    
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    
    event_id = Column(String, index=True) # ID do Pedido (Ex: 2824)
    event_name = Column(String)           # Ex: "Purchase"
    
    # Guarda o JSON completo que veio do site (Cookies, GCLID, IP, User Agent)
    payload_json = Column(JSONB)
    
    status = Column(String, default="PENDING") # PENDING, PROCESSED
    created_at = Column(DateTime, server_default=func.now())
    
    store = relationship("Store")