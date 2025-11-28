from sqlalchemy import Column, String, DateTime, Integer, Text
from sqlalchemy.sql import func
from core.database import Base

class ShopTraffic(Base):
    __tablename__ = "shop_traffic"

    id = Column(String(64), primary_key=True)
    owner_uid = Column(String(128), index=True, nullable=False)
    shop_uid = Column(String(128), index=True, nullable=True)
    slug = Column(String(255), index=True, nullable=True)

    path = Column(String(512), nullable=True)
    referrer = Column(Text, nullable=True)
    ip = Column(String(64), nullable=True)
    user_agent = Column(Text, nullable=True)
    device = Column(String(64), nullable=True)
    browser = Column(String(64), nullable=True)
    os = Column(String(64), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

