from sqlalchemy import Column, String, DateTime
from sqlalchemy.sql import func
from core.database import Base

class ShopDiscount(Base):
    __tablename__ = "shop_discounts"

    # Dodo discount id as primary key
    discount_id = Column(String(255), primary_key=True, index=True)
    owner_uid = Column(String(128), nullable=False, index=True)
    shop_uid = Column(String(128), nullable=True, index=True)
    slug = Column(String(255), nullable=True, index=True)
    code = Column(String(64), nullable=True)
    name = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

