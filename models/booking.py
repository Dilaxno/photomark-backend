"""
Booking System Models
Full booking/CRM system for photographers - stores clients, bookings, sessions, invoices
"""
from datetime import datetime
from typing import Optional, List
from sqlalchemy import Column, String, Text, DateTime, Boolean, Integer, Float, ForeignKey, JSON, Enum as SQLEnum
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import UUID
import uuid
import enum

from core.database import Base


class BookingStatus(str, enum.Enum):
    INQUIRY = "inquiry"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class PaymentStatus(str, enum.Enum):
    UNPAID = "unpaid"
    PARTIAL = "partial"
    PAID = "paid"
    REFUNDED = "refunded"


class SessionType(str, enum.Enum):
    PORTRAIT = "portrait"
    WEDDING = "wedding"
    EVENT = "event"
    COMMERCIAL = "commercial"
    FAMILY = "family"
    NEWBORN = "newborn"
    MATERNITY = "maternity"
    HEADSHOT = "headshot"
    PRODUCT = "product"
    REAL_ESTATE = "real_estate"
    OTHER = "other"


class Client(Base):
    """Client/Contact record"""
    __tablename__ = "booking_clients"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    uid = Column(String(128), nullable=False, index=True)  # Owner's Firebase UID
    
    # Basic info
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=True, index=True)
    phone = Column(String(50), nullable=True)
    
    # Additional details
    company = Column(String(255), nullable=True)
    address = Column(Text, nullable=True)
    city = Column(String(100), nullable=True)
    state = Column(String(100), nullable=True)
    zip_code = Column(String(20), nullable=True)
    country = Column(String(100), nullable=True)
    
    # Notes and tags
    notes = Column(Text, nullable=True)
    tags = Column(JSON, default=list)  # ["vip", "repeat", "referral"]
    
    # Source tracking
    source = Column(String(100), nullable=True)  # "website", "referral", "instagram", etc.
    referral_source = Column(String(255), nullable=True)
    
    # Avatar/photo
    avatar_url = Column(Text, nullable=True)
    
    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    bookings = relationship("Booking", back_populates="client", cascade="all, delete-orphan")
    
    def to_dict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "company": self.company,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "zip_code": self.zip_code,
            "country": self.country,
            "notes": self.notes,
            "tags": self.tags or [],
            "source": self.source,
            "referral_source": self.referral_source,
            "avatar_url": self.avatar_url,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class SessionPackage(Base):
    """Reusable session packages/pricing"""
    __tablename__ = "booking_packages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    uid = Column(String(128), nullable=False, index=True)
    
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    session_type = Column(SQLEnum(SessionType), default=SessionType.OTHER)
    
    # Pricing
    price = Column(Float, default=0.0)
    currency = Column(String(3), default="USD")
    deposit_amount = Column(Float, default=0.0)
    deposit_percentage = Column(Float, nullable=True)  # Alternative: percentage of total
    
    # Duration
    duration_minutes = Column(Integer, default=60)
    
    # Deliverables
    included_photos = Column(Integer, nullable=True)
    included_hours = Column(Float, nullable=True)
    deliverables = Column(JSON, default=list)  # ["20 edited photos", "Online gallery", "Print release"]
    
    # Settings
    is_active = Column(Boolean, default=True)
    color = Column(String(7), nullable=True)  # Hex color for UI
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "description": self.description,
            "session_type": self.session_type.value if self.session_type else None,
            "price": self.price,
            "currency": self.currency,
            "deposit_amount": self.deposit_amount,
            "deposit_percentage": self.deposit_percentage,
            "duration_minutes": self.duration_minutes,
            "included_photos": self.included_photos,
            "included_hours": self.included_hours,
            "deliverables": self.deliverables or [],
            "is_active": self.is_active,
            "color": self.color,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Booking(Base):
    """Main booking/session record"""
    __tablename__ = "bookings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    uid = Column(String(128), nullable=False, index=True)
    
    # Client reference
    client_id = Column(UUID(as_uuid=True), ForeignKey("booking_clients.id", ondelete="SET NULL"), nullable=True)
    client = relationship("Client", back_populates="bookings")
    
    # Quick client info (denormalized for display)
    client_name = Column(String(255), nullable=True)
    client_email = Column(String(255), nullable=True)
    client_phone = Column(String(50), nullable=True)
    
    # Session details
    title = Column(String(255), nullable=True)
    session_type = Column(SQLEnum(SessionType), default=SessionType.OTHER)
    package_id = Column(UUID(as_uuid=True), ForeignKey("booking_packages.id", ondelete="SET NULL"), nullable=True)
    
    # Scheduling
    session_date = Column(DateTime, nullable=True, index=True)
    session_end = Column(DateTime, nullable=True)
    duration_minutes = Column(Integer, default=60)
    timezone = Column(String(50), default="UTC")
    
    # Location
    location = Column(Text, nullable=True)
    location_address = Column(Text, nullable=True)
    location_notes = Column(Text, nullable=True)
    is_virtual = Column(Boolean, default=False)
    meeting_link = Column(Text, nullable=True)
    
    # Status
    status = Column(SQLEnum(BookingStatus), default=BookingStatus.INQUIRY, index=True)
    
    # Pricing
    total_amount = Column(Float, default=0.0)
    deposit_amount = Column(Float, default=0.0)
    amount_paid = Column(Float, default=0.0)
    currency = Column(String(3), default="USD")
    payment_status = Column(SQLEnum(PaymentStatus), default=PaymentStatus.UNPAID)
    
    # Notes
    notes = Column(Text, nullable=True)
    internal_notes = Column(Text, nullable=True)  # Private notes not shown to client
    
    # Questionnaire responses
    questionnaire_data = Column(JSON, default=dict)
    
    # Contract
    contract_signed = Column(Boolean, default=False)
    contract_signed_at = Column(DateTime, nullable=True)
    contract_id = Column(UUID(as_uuid=True), nullable=True)
    
    # Reminders
    reminder_sent = Column(Boolean, default=False)
    reminder_sent_at = Column(DateTime, nullable=True)
    
    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    payments = relationship("BookingPayment", back_populates="booking", cascade="all, delete-orphan")
    
    def to_dict(self):
        return {
            "id": str(self.id),
            "client_id": str(self.client_id) if self.client_id else None,
            "client_name": self.client_name,
            "client_email": self.client_email,
            "client_phone": self.client_phone,
            "title": self.title,
            "session_type": self.session_type.value if self.session_type else None,
            "package_id": str(self.package_id) if self.package_id else None,
            "session_date": self.session_date.isoformat() if self.session_date else None,
            "session_end": self.session_end.isoformat() if self.session_end else None,
            "duration_minutes": self.duration_minutes,
            "timezone": self.timezone,
            "location": self.location,
            "location_address": self.location_address,
            "location_notes": self.location_notes,
            "is_virtual": self.is_virtual,
            "meeting_link": self.meeting_link,
            "status": self.status.value if self.status else None,
            "total_amount": self.total_amount,
            "deposit_amount": self.deposit_amount,
            "amount_paid": self.amount_paid,
            "currency": self.currency,
            "payment_status": self.payment_status.value if self.payment_status else None,
            "notes": self.notes,
            "internal_notes": self.internal_notes,
            "questionnaire_data": self.questionnaire_data or {},
            "contract_signed": self.contract_signed,
            "contract_signed_at": self.contract_signed_at.isoformat() if self.contract_signed_at else None,
            "reminder_sent": self.reminder_sent,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class BookingPayment(Base):
    """Payment records for bookings"""
    __tablename__ = "booking_payments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    uid = Column(String(128), nullable=False, index=True)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False)
    
    # Payment details
    amount = Column(Float, nullable=False)
    currency = Column(String(3), default="USD")
    payment_type = Column(String(50), default="payment")  # "deposit", "payment", "final", "refund"
    payment_method = Column(String(50), nullable=True)  # "card", "cash", "check", "venmo", "paypal"
    
    # Status
    status = Column(String(50), default="completed")  # "pending", "completed", "failed", "refunded"
    
    # External reference
    external_id = Column(String(255), nullable=True)  # Stripe/PayPal transaction ID
    
    # Notes
    notes = Column(Text, nullable=True)
    
    # Timestamps
    paid_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationship
    booking = relationship("Booking", back_populates="payments")
    
    def to_dict(self):
        return {
            "id": str(self.id),
            "booking_id": str(self.booking_id),
            "amount": self.amount,
            "currency": self.currency,
            "payment_type": self.payment_type,
            "payment_method": self.payment_method,
            "status": self.status,
            "external_id": self.external_id,
            "notes": self.notes,
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class BookingSettings(Base):
    """User's booking settings (availability, defaults, etc.)"""
    __tablename__ = "booking_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    uid = Column(String(128), nullable=False, unique=True, index=True)
    
    # Business info
    business_name = Column(String(255), nullable=True)
    business_email = Column(String(255), nullable=True)
    business_phone = Column(String(50), nullable=True)
    
    # Availability (JSON: {"monday": {"enabled": true, "start": "09:00", "end": "17:00"}, ...})
    availability = Column(JSON, default=dict)
    
    # Booking settings
    default_duration = Column(Integer, default=60)
    buffer_before = Column(Integer, default=15)  # Minutes before session
    buffer_after = Column(Integer, default=15)   # Minutes after session
    min_notice_hours = Column(Integer, default=24)  # Minimum booking notice
    max_advance_days = Column(Integer, default=90)  # How far in advance can book
    
    # Default pricing
    default_currency = Column(String(3), default="USD")
    default_deposit_percentage = Column(Float, default=25.0)
    
    # Notifications
    email_notifications = Column(Boolean, default=True)
    sms_notifications = Column(Boolean, default=False)
    
    # Booking page
    booking_page_enabled = Column(Boolean, default=False)
    booking_page_slug = Column(String(100), nullable=True, unique=True)
    booking_page_title = Column(String(255), nullable=True)
    booking_page_description = Column(Text, nullable=True)
    
    # Timezone
    timezone = Column(String(50), default="America/New_York")
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        return {
            "id": str(self.id),
            "business_name": self.business_name,
            "business_email": self.business_email,
            "business_phone": self.business_phone,
            "availability": self.availability or {},
            "default_duration": self.default_duration,
            "buffer_before": self.buffer_before,
            "buffer_after": self.buffer_after,
            "min_notice_hours": self.min_notice_hours,
            "max_advance_days": self.max_advance_days,
            "default_currency": self.default_currency,
            "default_deposit_percentage": self.default_deposit_percentage,
            "email_notifications": self.email_notifications,
            "sms_notifications": self.sms_notifications,
            "booking_page_enabled": self.booking_page_enabled,
            "booking_page_slug": self.booking_page_slug,
            "booking_page_title": self.booking_page_title,
            "booking_page_description": self.booking_page_description,
            "timezone": self.timezone,
        }
