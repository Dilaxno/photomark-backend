from sqlalchemy import Column, String, Text, DateTime, Boolean, JSON
from sqlalchemy.sql import func
from core.database import Base


class ChatWorkspace(Base):
    """Stores chat workspaces for team communication between owner and collaborators."""
    __tablename__ = "chat_workspaces"

    id = Column(String(128), primary_key=True, index=True)  # Workspace ID (ws_timestamp_random)
    owner_uid = Column(String(128), index=True, nullable=False)  # Owner who created this workspace
    name = Column(String(255), nullable=False)  # Workspace display name
    type = Column(String(20), nullable=False, default="direct")  # 'direct' or 'group'
    members = Column(JSON, nullable=False, default=list)  # List of member emails
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_message_at = Column(DateTime(timezone=True), nullable=True)
    active = Column(Boolean, default=True, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "owner_uid": self.owner_uid,
            "name": self.name,
            "type": self.type,
            "members": self.members or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
        }
