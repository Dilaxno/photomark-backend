"""
Pricing models for PostgreSQL (Neon)
- pricing_events: raw webhook/audit events
- subscriptions: current subscription snapshot
- invoices: user billing invoices
"""
from sqlalchemy import Column, Integer, String, DateTime, Text, Numeric
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


class Invoice(Base):
    """User billing invoices from payment provider."""
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    invoice_id = Column(String(255), nullable=False, unique=True, index=True)  # External invoice/payment ID
    user_uid = Column(String(128), nullable=False, index=True)
    payment_id = Column(String(255), nullable=True)  # Payment provider's payment ID
    subscription_id = Column(String(255), nullable=True)  # Associated subscription ID
    amount = Column(Numeric(10, 2), nullable=False, default=0)  # Amount in dollars
    currency = Column(String(10), nullable=False, default="USD")
    status = Column(String(50), nullable=False, default="paid")  # paid, pending, failed, refunded
    plan = Column(String(50), nullable=True)  # Plan slug (individual, studios, golden)
    plan_display = Column(String(255), nullable=True)  # Human-readable plan name
    billing_cycle = Column(String(20), nullable=True)  # monthly, yearly
    download_url = Column(Text, nullable=True)  # Invoice/receipt URL from payment provider
    invoice_date = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PaymentMethod(Base):
    """User saved payment methods from payment provider."""
    __tablename__ = "payment_methods"

    id = Column(Integer, primary_key=True, autoincrement=True)
    payment_method_id = Column(String(255), nullable=False, index=True)  # External payment method ID
    user_uid = Column(String(128), nullable=False, index=True)
    type = Column(String(50), nullable=False, default="card")  # card, visa, mastercard, amex, paypal, etc.
    last4 = Column(String(4), nullable=True)  # Last 4 digits of card
    expiry_month = Column(String(2), nullable=True)  # MM
    expiry_year = Column(String(4), nullable=True)  # YYYY or YY
    expiry = Column(String(10), nullable=True)  # MM/YY formatted
    brand = Column(String(50), nullable=True)  # Card brand (Visa, Mastercard, etc.)
    is_default = Column(Integer, nullable=False, default=0)  # 1 = default, 0 = not default
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
