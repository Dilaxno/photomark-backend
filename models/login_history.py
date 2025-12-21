"""
Login history model for tracking user login sessions
"""
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Boolean
from sqlalchemy.sql import func
from core.database import Base


class LoginHistory(Base):
    __tablename__ = "login_history"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    uid = Column(String(128), ForeignKey("users.uid", ondelete="CASCADE"), nullable=True, index=True)  # nullable for failed logins
    
    # IP and location info
    ip_address = Column(String(45), nullable=False)  # IPv6 max length
    city = Column(String(100), nullable=True)
    country = Column(String(100), nullable=True)
    country_code = Column(String(2), nullable=True)  # ISO 3166-1 alpha-2 code for flags
    
    # Request metadata
    user_agent = Column(String(512), nullable=True)  # Browser/client user agent
    source = Column(String(50), nullable=False, default="web")  # web, lightroom, photoshop, api
    
    # Login result
    success = Column(Boolean, nullable=False, default=True)
    failure_reason = Column(String(255), nullable=True)  # For failed logins: invalid_password, account_locked, etc.
    
    # Email for failed logins (when uid is not available)
    attempted_email = Column(String(255), nullable=True)
    
    # Timestamp
    logged_in_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    def to_dict(self):
        return {
            "id": self.id,
            "ip_address": self.ip_address,
            "city": self.city,
            "country": self.country,
            "country_code": self.country_code,
            "user_agent": self.user_agent,
            "source": self.source,
            "success": self.success,
            "logged_in_at": self.logged_in_at.isoformat() if self.logged_in_at else None,
        }
