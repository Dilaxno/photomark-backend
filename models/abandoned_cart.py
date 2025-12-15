"""
Abandoned Cart Model
Tracks cart sessions for abandoned cart recovery emails
"""
from sqlalchemy import Column, String, Text, JSON, DateTime, Integer, Boolean
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import UUID
import uuid
from core.database import Base


class AbandonedCart(Base):
    """
    Tracks cart sessions for abandoned cart recovery.
    A cart is considered abandoned if:
    - Customer added items but didn't complete checkout
    - More than 1 hour has passed since last activity
    - No purchase was completed for this session
    """
    __tablename__ = "abandoned_carts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Shop info
    shop_uid = Column(String(128), index=True, nullable=False)
    shop_slug = Column(String(255), index=True, nullable=True)
    
    # Customer info (captured when they start checkout)
    customer_email = Column(String(255), index=True, nullable=True)
    customer_name = Column(String(255), nullable=True)
    
    # Session tracking
    session_id = Column(String(128), index=True, nullable=False)  # Browser session ID
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    
    # Cart contents
    items = Column(JSON, nullable=False, default=[])  # [{id, title, quantity, price_cents, currency, image_url}]
    cart_total_cents = Column(Integer, default=0)
    currency = Column(String(10), default="USD")
    
    # Recovery tracking
    recovery_email_sent = Column(Boolean, default=False)
    recovery_email_sent_at = Column(DateTime(timezone=True), nullable=True)
    recovery_email_count = Column(Integer, default=0)  # How many reminder emails sent
    
    # Conversion tracking
    converted = Column(Boolean, default=False)  # Did they complete purchase?
    converted_at = Column(DateTime(timezone=True), nullable=True)
    conversion_payment_id = Column(String(128), nullable=True)
    
    # Recovery link
    recovery_token = Column(String(64), unique=True, index=True, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_activity_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    def to_dict(self):
        return {
            "id": str(self.id),
            "shop_uid": self.shop_uid,
            "shop_slug": self.shop_slug,
            "customer_email": self.customer_email,
            "customer_name": self.customer_name,
            "items": self.items or [],
            "cart_total_cents": self.cart_total_cents,
            "currency": self.currency,
            "recovery_email_sent": self.recovery_email_sent,
            "recovery_email_count": self.recovery_email_count,
            "converted": self.converted,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_activity_at": self.last_activity_at.isoformat() if self.last_activity_at else None,
        }
