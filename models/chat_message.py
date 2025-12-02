from sqlalchemy import Column, String, Text, DateTime, Integer, JSON
from sqlalchemy.sql import func
from core.database import Base


class ChatMessage(Base):
    """Stores chat messages between owner and collaborators, secured by owner_uid."""
    __tablename__ = "chat_messages"

    id = Column(String(64), primary_key=True, index=True)
    owner_uid = Column(String(128), index=True, nullable=False)  # Owner who owns this chat channel
    channel_id = Column(String(128), index=True, nullable=False)  # Channel ID (collab_{owner_uid})
    sender_id = Column(String(128), index=True, nullable=False)  # User ID of sender
    sender_name = Column(String(255), nullable=True)
    sender_image = Column(String(512), nullable=True)
    text = Column(Text, nullable=True)
    attachments = Column(JSON, nullable=True)  # Store attachment metadata as JSON
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "owner_uid": self.owner_uid,
            "channel_id": self.channel_id,
            "user": {
                "id": self.sender_id,
                "name": self.sender_name,
                "image": self.sender_image,
            },
            "text": self.text or "",
            "attachments": self.attachments or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
