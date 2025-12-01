from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func
from core.database import Base


class GalleryAsset(Base):
    __tablename__ = "gallery_assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_uid = Column(String(128), index=True, nullable=False)
    vault = Column(String(255), index=True, nullable=True)
    key = Column(Text, unique=True, nullable=False)
    size_bytes = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

