"""
Prelaunch/Waitlist signup model for PostgreSQL
"""
from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func
from core.database import Base

class PrelaunchSignup(Base):
    __tablename__ = "prelaunch_signups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), nullable=False, unique=True, index=True)
    source = Column(String(255), nullable=True)
    ip = Column(String(64), nullable=True)
    user_agent = Column(String(512), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
