"""
Login history model for tracking user login sessions
"""
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey
from sqlalchemy.sql import func
from core.database import Base


class LoginHistory(Base):
    __tablename__ = "login_history"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    uid = Column(String(128), ForeignKey("users.uid", ondelete="CASCADE"), nullable=False, index=True)
    
    # IP and location info
    ip_address = Column(String(45), nullable=False)  # IPv6 max length
    city = Column(String(100), nullable=True)
    country = Column(String(100), nullable=True)
    country_code = Column(String(2), nullable=True)  # ISO 3166-1 alpha-2 code for flags
    
    # Timestamp
    logged_in_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    def to_dict(self):
        return {
            "id": self.id,
            "ip_address": self.ip_address,
            "city": self.city,
            "country": self.country,
            "country_code": self.country_code,
            "logged_in_at": self.logged_in_at.isoformat() if self.logged_in_at else None,
        }
