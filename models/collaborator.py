from sqlalchemy import Column, String, Boolean, DateTime
from sqlalchemy.sql import func
from core.database import Base


class Collaborator(Base):
    __tablename__ = "collaborators"

    id = Column(String(64), primary_key=True, index=True)
    owner_uid = Column(String(128), index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=True)
    role = Column(String(64), nullable=False)
    password_hash = Column(String(255), nullable=False)
    active = Column('is_active', Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_login_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "owner_uid": self.owner_uid,
            "email": self.email,
            "name": self.name,
            "role": self.role,
            "active": bool(self.active),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }

