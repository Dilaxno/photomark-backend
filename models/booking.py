"""
Booking System Models
Full booking/CRM system for photographers - stores clients, bookings, sessions, invoices
Enhanced with CleanEnroll-style form builder field types and validations
"""
from datetime import datetime
from typing import Optional, List
from sqlalchemy import Column, String, Text, DateTime, Boolean, Integer, Float, ForeignKey, JSON, Enum as SQLEnum, Index
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


# ============================================================================
# FIELD TYPES - Matching CleanEnroll's comprehensive field type system
# ============================================================================

# All supported field types for booking forms
FIELD_TYPES = [
    # Headings (display only)
    "heading1", "heading2", "heading3",
    # Basic Information
    "full-name", "email", "phone", "country", "address", "age",
    # Text & Input
    "text", "textarea", "long-answer", "password", "signature",
    # Choice & Selection
    "yes-no", "dropdown", "multiple", "checkbox", "tags", "color-picker", "ranking", "quiz",
    # Date & Time
    "date", "time",
    # Rating & Scale
    "linear-scale", "rating-stars", "rating-heart", "rating-number", "rating-emoji", "range-slider", "matrix",
    # Numeric & Quantitative
    "number", "price", "payment", "dimensions",
    # Upload & Recording
    "file", "video-recording", "audio-recording",
    # Media Displays (non-input)
    "image", "video", "audio",
    # Link & Location
    "url", "location", "my-location",
]

# Field types that don't collect input (display only)
DISPLAY_ONLY_FIELDS = ["heading1", "heading2", "heading3", "image", "video", "audio"]

# Field types that require options array
FIELDS_WITH_OPTIONS = ["dropdown", "multiple", "checkbox", "tags", "ranking", "quiz"]


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
    source = Column(String(100), nullable=True)
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
    deposit_percentage = Column(Float, nullable=True)
    
    # Duration
    duration_minutes = Column(Integer, default=60)
    
    # Deliverables
    included_photos = Column(Integer, nullable=True)
    included_hours = Column(Float, nullable=True)
    deliverables = Column(JSON, default=list)
    
    # Settings
    is_active = Column(Boolean, default=True)
    color = Column(String(7), nullable=True)
    
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
    
    # Quick client info (denormalized)
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
    internal_notes = Column(Text, nullable=True)
    
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
    
    amount = Column(Float, nullable=False)
    currency = Column(String(3), default="USD")
    payment_type = Column(String(50), default="payment")
    payment_method = Column(String(50), nullable=True)
    status = Column(String(50), default="completed")
    external_id = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    paid_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
    
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
    """User's booking settings (availability, defaults, branding)"""
    __tablename__ = "booking_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    uid = Column(String(128), nullable=False, unique=True, index=True)
    
    # Business info
    business_name = Column(String(255), nullable=True)
    business_email = Column(String(255), nullable=True)
    business_phone = Column(String(50), nullable=True)
    business_logo = Column(Text, nullable=True)
    business_website = Column(Text, nullable=True)
    
    # Availability JSON
    availability = Column(JSON, default=dict)
    
    # Booking settings
    default_duration = Column(Integer, default=60)
    buffer_before = Column(Integer, default=15)
    buffer_after = Column(Integer, default=15)
    min_notice_hours = Column(Integer, default=24)
    max_advance_days = Column(Integer, default=90)
    
    # Default pricing
    default_currency = Column(String(3), default="USD")
    default_deposit_percentage = Column(Float, default=25.0)
    
    # Notifications
    email_notifications = Column(Boolean, default=True)
    sms_notifications = Column(Boolean, default=False)
    
    # Branding
    brand_logo = Column(Text, nullable=True)
    brand_primary_color = Column(String(7), default="#6366f1")
    brand_secondary_color = Column(String(7), default="#8b5cf6")
    brand_text_color = Column(String(7), default="#1f2937")
    brand_background_color = Column(String(7), default="#ffffff")
    
    # Booking page
    booking_page_enabled = Column(Boolean, default=False)
    booking_page_slug = Column(String(100), nullable=True, unique=True)
    booking_page_title = Column(String(255), nullable=True)
    booking_page_description = Column(Text, nullable=True)
    booking_page_cover_image = Column(Text, nullable=True)
    booking_page_theme = Column(String(50), default="light")
    booking_page_accent_color = Column(String(7), default="#6366f1")
    
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
            "business_logo": self.business_logo,
            "brand_logo": self.brand_logo,
            "brand_primary_color": self.brand_primary_color,
            "brand_secondary_color": self.brand_secondary_color,
            "brand_text_color": self.brand_text_color,
            "brand_background_color": self.brand_background_color,
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


class BookingForm(Base):
    """
    Custom booking forms with CleanEnroll-style drag-and-drop fields
    Enhanced with comprehensive field types, validation, and theming
    """
    __tablename__ = "booking_forms"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    uid = Column(String(128), nullable=False, index=True)
    
    # Form basics
    name = Column(String(255), nullable=False)
    slug = Column(String(100), nullable=True, index=True)
    description = Column(Text, nullable=True)
    
    # Form content
    title = Column(String(255), nullable=True)
    subtitle = Column(Text, nullable=True)
    
    # Fields array - each field has: {id, type, label, required, placeholder, options, validation, etc.}
    fields = Column(JSON, default=list)
    
    # Form type: 'simple' (single page) or 'multi-step' (wizard)
    form_type = Column(String(20), default="simple")
    
    # Language
    language = Column(String(10), default="en")
    
    # ============================================================================
    # THEME/STYLING - Matching CleanEnroll's comprehensive theming
    # ============================================================================
    theme = Column(JSON, default=dict)  # Full theme object
    # Individual theme fields for quick access
    primary_color = Column(String(7), default="#4f46e5")
    background_color = Column(String(7), default="#ffffff")
    text_color = Column(String(7), default="#111827")
    input_bg_color = Column(String(7), default="#ffffff")
    input_border_color = Column(String(7), default="#d1d5db")
    input_border_radius = Column(Integer, default=8)
    font_family = Column(String(100), default="Inter")
    layout_variant = Column(String(20), default="card")  # card, split, isolated, full-page
    
    # Branding
    branding = Column(JSON, default=dict)  # {logo, logoPosition, logoSize}

    # ============================================================================
    # SUBMIT BUTTON CUSTOMIZATION
    # ============================================================================
    submit_button_text = Column(String(100), default="Submit")
    submit_button_color = Column(String(7), default="#3b82f6")
    submit_button_text_color = Column(String(7), default="#ffffff")
    submit_button_position = Column(String(20), default="left")  # left, center, right
    
    # ============================================================================
    # SUCCESS/THANK YOU SETTINGS
    # ============================================================================
    success_message = Column(Text, default="Thank you for your submission!")
    thank_you_display = Column(String(20), default="message")  # message, redirect, toast
    celebration_enabled = Column(Boolean, default=False)
    redirect_url = Column(Text, nullable=True)
    redirect_enabled = Column(Boolean, default=False)
    
    # ============================================================================
    # AUTO-REPLY EMAIL SETTINGS
    # ============================================================================
    auto_reply_enabled = Column(Boolean, default=False)
    auto_reply_email_field_id = Column(String(100), nullable=True)  # Which field contains email
    auto_reply_subject = Column(String(255), default="Thank you for contacting us")
    auto_reply_message_html = Column(Text, nullable=True)
    auto_reply_message_text = Column(Text, nullable=True)
    
    # ============================================================================
    # EMAIL VALIDATION SETTINGS (CleanEnroll-style)
    # ============================================================================
    email_validation_enabled = Column(Boolean, default=False)
    professional_emails_only = Column(Boolean, default=False)  # Block free email providers
    block_role_emails = Column(Boolean, default=False)  # Block admin@, info@, etc.
    email_reject_bad_reputation = Column(Boolean, default=False)
    min_domain_age_days = Column(Integer, default=30)
    verify_email_domain = Column(Boolean, default=False)  # MX record check
    detect_gibberish_email = Column(Boolean, default=False)

    # ============================================================================
    # BOT PROTECTION & SPAM PREVENTION (CleanEnroll-style)
    # ============================================================================
    honeypot_enabled = Column(Boolean, default=False)
    time_based_check_enabled = Column(Boolean, default=False)
    min_submission_time = Column(Integer, default=3)  # Minimum seconds to fill form
    recaptcha_enabled = Column(Boolean, default=False)
    recaptcha_site_key = Column(String(255), nullable=True)
    
    # ============================================================================
    # DUPLICATE PREVENTION
    # ============================================================================
    prevent_duplicate_email = Column(Boolean, default=False)
    prevent_duplicate_by_ip = Column(Boolean, default=False)
    duplicate_window_hours = Column(Integer, default=24)
    
    # ============================================================================
    # GEO RESTRICTIONS
    # ============================================================================
    restricted_countries = Column(JSON, default=list)  # Countries to block
    allowed_countries = Column(JSON, default=list)  # Only allow these countries
    
    # ============================================================================
    # PASSWORD PROTECTION
    # ============================================================================
    password_protection_enabled = Column(Boolean, default=False)
    password_hash = Column(String(255), nullable=True)
    
    # ============================================================================
    # GDPR & PRIVACY
    # ============================================================================
    gdpr_compliance_enabled = Column(Boolean, default=False)
    privacy_policy_url = Column(Text, nullable=True)
    show_powered_by = Column(Boolean, default=True)
    
    # ============================================================================
    # SUBMISSION LIMITS
    # ============================================================================
    submission_limit = Column(Integer, default=0)  # 0 = unlimited
    
    # Notifications
    notify_email = Column(String(255), nullable=True)
    send_confirmation = Column(Boolean, default=True)

    # Status
    is_active = Column(Boolean, default=True)
    is_published = Column(Boolean, default=False)
    
    # Stats
    views_count = Column(Integer, default=0)
    submissions_count = Column(Integer, default=0)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    submissions = relationship("FormSubmission", back_populates="form", cascade="all, delete-orphan")
    
    def to_dict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "title": self.title,
            "subtitle": self.subtitle,
            "fields": self.fields or [],
            "form_type": self.form_type,
            "language": self.language,
            # Theme
            "theme": self.theme or {},
            "primary_color": self.primary_color,
            "background_color": self.background_color,
            "text_color": self.text_color,
            "input_bg_color": self.input_bg_color,
            "input_border_color": self.input_border_color,
            "input_border_radius": self.input_border_radius,
            "font_family": self.font_family,
            "layout_variant": self.layout_variant,
            "branding": self.branding or {},
            # Submit button
            "submit_button_text": self.submit_button_text,
            "submit_button_color": self.submit_button_color,
            "submit_button_text_color": self.submit_button_text_color,
            "submit_button_position": self.submit_button_position,
            # Success settings
            "success_message": self.success_message,
            "thank_you_display": self.thank_you_display,
            "celebration_enabled": self.celebration_enabled,
            "redirect_url": self.redirect_url,
            "redirect_enabled": self.redirect_enabled,
            # Auto-reply
            "auto_reply_enabled": self.auto_reply_enabled,
            "auto_reply_email_field_id": self.auto_reply_email_field_id,
            "auto_reply_subject": self.auto_reply_subject,
            # Email validation
            "email_validation_enabled": self.email_validation_enabled,
            "professional_emails_only": self.professional_emails_only,
            "block_role_emails": self.block_role_emails,
            "email_reject_bad_reputation": self.email_reject_bad_reputation,
            "min_domain_age_days": self.min_domain_age_days,
            "verify_email_domain": self.verify_email_domain,
            "detect_gibberish_email": self.detect_gibberish_email,
            # Bot protection
            "honeypot_enabled": self.honeypot_enabled,
            "time_based_check_enabled": self.time_based_check_enabled,
            "min_submission_time": self.min_submission_time,
            "recaptcha_enabled": self.recaptcha_enabled,
            # Duplicate prevention
            "prevent_duplicate_email": self.prevent_duplicate_email,
            "prevent_duplicate_by_ip": self.prevent_duplicate_by_ip,
            "duplicate_window_hours": self.duplicate_window_hours,
            # Geo restrictions
            "restricted_countries": self.restricted_countries or [],
            "allowed_countries": self.allowed_countries or [],
            # Password protection
            "password_protection_enabled": self.password_protection_enabled,
            # GDPR
            "gdpr_compliance_enabled": self.gdpr_compliance_enabled,
            "privacy_policy_url": self.privacy_policy_url,
            "show_powered_by": self.show_powered_by,
            # Limits
            "submission_limit": self.submission_limit,
            # Notifications
            "notify_email": self.notify_email,
            "send_confirmation": self.send_confirmation,
            # Status
            "is_active": self.is_active,
            "is_published": self.is_published,
            "views_count": self.views_count,
            "submissions_count": self.submissions_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class FormSubmission(Base):
    """Submissions from booking forms with enhanced metadata"""
    __tablename__ = "form_submissions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    uid = Column(String(128), nullable=False, index=True)
    form_id = Column(UUID(as_uuid=True), ForeignKey("booking_forms.id", ondelete="CASCADE"), nullable=False)
    
    # Submission data
    data = Column(JSON, default=dict)
    
    # Contact info (extracted for quick access)
    contact_name = Column(String(255), nullable=True)
    contact_email = Column(String(255), nullable=True, index=True)
    contact_phone = Column(String(50), nullable=True)
    
    # Calendar booking
    scheduled_date = Column(DateTime, nullable=True, index=True)
    scheduled_end = Column(DateTime, nullable=True)
    
    # Status
    status = Column(String(50), default="new")  # new, read, contacted, converted, archived
    
    # Metadata
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    referrer = Column(Text, nullable=True)
    country_code = Column(String(2), nullable=True)
    
    # Spam/validation flags
    spam_score = Column(Float, default=0.0)
    is_spam = Column(Boolean, default=False)
    validation_errors = Column(JSON, default=list)
    
    # Time tracking (for bot detection)
    form_load_time = Column(DateTime, nullable=True)
    submission_time = Column(DateTime, nullable=True)
    time_to_complete_seconds = Column(Integer, nullable=True)
    
    # Linked booking
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    form = relationship("BookingForm", back_populates="submissions")

    def to_dict(self):
        return {
            "id": str(self.id),
            "form_id": str(self.form_id),
            "data": self.data or {},
            "contact_name": self.contact_name,
            "contact_email": self.contact_email,
            "contact_phone": self.contact_phone,
            "scheduled_date": self.scheduled_date.isoformat() if self.scheduled_date else None,
            "scheduled_end": self.scheduled_end.isoformat() if self.scheduled_end else None,
            "status": self.status,
            "ip_address": self.ip_address,
            "country_code": self.country_code,
            "spam_score": self.spam_score,
            "is_spam": self.is_spam,
            "time_to_complete_seconds": self.time_to_complete_seconds,
            "booking_id": str(self.booking_id) if self.booking_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class FormView(Base):
    """Track unique form views"""
    __tablename__ = "form_views"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    form_id = Column(UUID(as_uuid=True), ForeignKey("booking_forms.id", ondelete="CASCADE"), nullable=False, index=True)
    visitor_hash = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index('ix_form_views_form_visitor', 'form_id', 'visitor_hash'),
    )


class MiniSession(Base):
    """Mini-session events with multiple time slots"""
    __tablename__ = "mini_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    uid = Column(String(128), nullable=False, index=True)
    
    name = Column(String(255), nullable=False)
    slug = Column(String(100), nullable=True, index=True)
    description = Column(Text, nullable=True)
    
    session_type = Column(SQLEnum(SessionType), default=SessionType.PORTRAIT)
    duration_minutes = Column(Integer, default=20)
    buffer_minutes = Column(Integer, default=10)
    
    price = Column(Float, default=0.0)
    deposit_amount = Column(Float, default=0.0)
    currency = Column(String(3), default="USD")
    
    included_photos = Column(Integer, nullable=True)
    deliverables = Column(JSON, default=list)
    
    location_name = Column(String(255), nullable=True)
    location_address = Column(Text, nullable=True)
    location_notes = Column(Text, nullable=True)
    
    cover_image = Column(Text, nullable=True)
    gallery_images = Column(JSON, default=list)
    
    max_bookings_per_slot = Column(Integer, default=1)
    allow_waitlist = Column(Boolean, default=True)
    require_deposit = Column(Boolean, default=True)
    auto_confirm = Column(Boolean, default=False)
    
    is_active = Column(Boolean, default=True)
    is_published = Column(Boolean, default=False)
    views_count = Column(Integer, default=0)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    dates = relationship("MiniSessionDate", back_populates="mini_session", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "session_type": self.session_type.value if self.session_type else None,
            "duration_minutes": self.duration_minutes,
            "buffer_minutes": self.buffer_minutes,
            "price": self.price,
            "deposit_amount": self.deposit_amount,
            "currency": self.currency,
            "included_photos": self.included_photos,
            "deliverables": self.deliverables or [],
            "location_name": self.location_name,
            "location_address": self.location_address,
            "location_notes": self.location_notes,
            "cover_image": self.cover_image,
            "gallery_images": self.gallery_images or [],
            "max_bookings_per_slot": self.max_bookings_per_slot,
            "allow_waitlist": self.allow_waitlist,
            "require_deposit": self.require_deposit,
            "auto_confirm": self.auto_confirm,
            "is_active": self.is_active,
            "is_published": self.is_published,
            "views_count": self.views_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class MiniSessionDate(Base):
    """Specific dates for a mini-session event"""
    __tablename__ = "mini_session_dates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    uid = Column(String(128), nullable=False, index=True)
    mini_session_id = Column(UUID(as_uuid=True), ForeignKey("mini_sessions.id", ondelete="CASCADE"), nullable=False)
    
    session_date = Column(DateTime, nullable=False, index=True)
    location_name = Column(String(255), nullable=True)
    location_address = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    mini_session = relationship("MiniSession", back_populates="dates")
    slots = relationship("MiniSessionSlot", back_populates="session_date", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": str(self.id),
            "mini_session_id": str(self.mini_session_id),
            "session_date": self.session_date.isoformat() if self.session_date else None,
            "location_name": self.location_name,
            "location_address": self.location_address,
            "notes": self.notes,
            "is_active": self.is_active,
            "slots": [s.to_dict() for s in self.slots] if self.slots else [],
        }


class MiniSessionSlot(Base):
    """Individual time slots within a mini-session date"""
    __tablename__ = "mini_session_slots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    uid = Column(String(128), nullable=False, index=True)
    session_date_id = Column(UUID(as_uuid=True), ForeignKey("mini_session_dates.id", ondelete="CASCADE"), nullable=False)
    
    start_time = Column(DateTime, nullable=False, index=True)
    end_time = Column(DateTime, nullable=False)
    status = Column(String(50), default="available")
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True)
    held_until = Column(DateTime, nullable=True)
    held_by_email = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    session_date = relationship("MiniSessionDate", back_populates="slots")
    
    def to_dict(self):
        return {
            "id": str(self.id),
            "session_date_id": str(self.session_date_id),
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "status": self.status,
            "booking_id": str(self.booking_id) if self.booking_id else None,
            "is_available": self.status == "available" and (not self.held_until or self.held_until < datetime.utcnow()),
        }


class Waitlist(Base):
    """Waitlist entries for fully booked sessions"""
    __tablename__ = "booking_waitlist"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    uid = Column(String(128), nullable=False, index=True)
    
    mini_session_id = Column(UUID(as_uuid=True), ForeignKey("mini_sessions.id", ondelete="CASCADE"), nullable=True)
    session_date_id = Column(UUID(as_uuid=True), ForeignKey("mini_session_dates.id", ondelete="CASCADE"), nullable=True)
    
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False, index=True)
    phone = Column(String(50), nullable=True)
    
    preferred_dates = Column(JSON, default=list)
    preferred_times = Column(JSON, default=list)
    notes = Column(Text, nullable=True)
    
    status = Column(String(50), default="waiting")
    notified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            "id": str(self.id),
            "mini_session_id": str(self.mini_session_id) if self.mini_session_id else None,
            "session_date_id": str(self.session_date_id) if self.session_date_id else None,
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "preferred_dates": self.preferred_dates or [],
            "preferred_times": self.preferred_times or [],
            "notes": self.notes,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
