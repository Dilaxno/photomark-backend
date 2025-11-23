"""
Replies (inbound email responses / comments) models for PostgreSQL
"""
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey
from sqlalchemy.sql import func
from core.database import Base

class Reply(Base):
    __tablename__ = "replies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_uid = Column(String(128), nullable=False, index=True)
    target_id = Column(String(255), nullable=False, index=True)
    target_type = Column(String(50), nullable=True)

    from_email = Column(String(255), nullable=False)
    from_name = Column(String(255), nullable=True)
    subject = Column(Text, nullable=True)
    text = Column(Text, nullable=True)
    html = Column(Text, nullable=True)

    parent_id = Column(Integer, nullable=True, index=True)
    is_deleted = Column(Boolean, default=False)

    ts = Column(Integer, nullable=False, index=True)  # epoch seconds for filtering/cursors
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
