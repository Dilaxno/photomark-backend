"""
Pricing models for PostgreSQL (Neon)
- pricing_events: raw webhook/audit events
- subscriptions: current subscription snapshot
"""
from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from core.database import Base

class PricingEvent(Base):
    __tablename__ = "pricing_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_uid = Column(String(128), nullable=True, index=True)
    provider = Column(String(50), nullable=False, default="dodo")
    event_type = Column(String(100), nullable=False)
    event_id = Column(String(255), nullable=True)
    payload = Column(JSONB, nullable=False, server_default='{}')
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_uid = Column(String(128), nullable=False, unique=True, index=True)
    provider = Column(String(50), nullable=False, default="dodo")
    plan = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False)
    current_period_end = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
