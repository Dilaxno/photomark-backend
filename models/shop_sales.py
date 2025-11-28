from sqlalchemy import Column, String, Text, JSON, DateTime, Integer, Boolean
from sqlalchemy.sql import func
from core.database import Base

class ShopSale(Base):
    """
    Records each completed shop sale (one record per successful payment).
    Ensures accurate owner earnings tracking based on webhook data.
    """
    __tablename__ = "shop_sales"

    id = Column(String(64), primary_key=True)  # payment_id when available, else synthesized
    owner_uid = Column(String(128), index=True, nullable=False)
    shop_uid = Column(String(128), index=True, nullable=True)
    slug = Column(String(255), index=True, nullable=True)

    payment_id = Column(String(128), unique=True, index=True, nullable=True)
    customer_email = Column(String(255), nullable=True)

    currency = Column(String(10), nullable=False, default="USD")
    amount_cents = Column(Integer, nullable=False, default=0)

    items = Column(JSON, nullable=False, default=[])  # [{id,title,quantity,unit_price_cents,line_total_cents,currency}]
    sale_metadata = Column('metadata', JSON, nullable=False, default={})

    delivered = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
