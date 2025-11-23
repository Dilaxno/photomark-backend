"""
Affiliate models for PostgreSQL (Neon)
Replaces Firestore collections:
- affiliate_profiles
- affiliate_attributions
- affiliate_conversions
"""
from sqlalchemy import Column, String, Integer, Boolean, DateTime, Text
from sqlalchemy.sql import func
from core.database import Base

class AffiliateProfile(Base):
    __tablename__ = "affiliate_profiles"

    # Primary key: affiliate uid (same as User.uid)
    uid = Column(String(128), primary_key=True, index=True)

    # Identity
    platform = Column(String(100), nullable=True)
    channel = Column(String(255), nullable=True)
    email = Column(String(255), nullable=True)
    name = Column(String(255), nullable=True)

    # Referral
    referral_code = Column(String(255), unique=True, index=True, nullable=False)
    referral_link = Column(Text, nullable=False)

    # Aggregate counters
    clicks_total = Column(Integer, default=0)
    signups_total = Column(Integer, default=0)
    conversions_total = Column(Integer, default=0)
    gross_cents_total = Column(Integer, default=0)
    payout_cents_total = Column(Integer, default=0)

    # Last activity timestamps
    last_click_at = Column(DateTime(timezone=True), nullable=True)
    last_signup_at = Column(DateTime(timezone=True), nullable=True)
    last_conversion_at = Column(DateTime(timezone=True), nullable=True)

    # Audit
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AffiliateAttribution(Base):
    __tablename__ = "affiliate_attributions"

    id = Column(Integer, primary_key=True, autoincrement=True)

    affiliate_uid = Column(String(128), index=True, nullable=False)
    user_uid = Column(String(128), unique=True, index=True, nullable=False)  # one attribution per user
    ref = Column(String(255), nullable=True)

    verified = Column(Boolean, default=False)
    attributed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    verified_at = Column(DateTime(timezone=True), nullable=True)


class AffiliateConversion(Base):
    __tablename__ = "affiliate_conversions"

    id = Column(Integer, primary_key=True, autoincrement=True)

    affiliate_uid = Column(String(128), index=True, nullable=False)
    user_uid = Column(String(128), index=True, nullable=True)

    amount_cents = Column(Integer, default=0)
    payout_cents = Column(Integer, default=0)
    currency = Column(String(10), default="usd")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    conversion_date = Column(DateTime(timezone=True), nullable=True)
