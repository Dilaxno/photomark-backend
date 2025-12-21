"""
Login tracking utility - reusable across auth methods
Tracks login attempts (success and failure) with IP, user agent, and source
"""
import asyncio
from datetime import datetime
from typing import Optional, Literal
from fastapi import Request
import httpx

from core.config import logger
from core.database import SessionLocal
from models.login_history import LoginHistory
from models.user import User


LoginSource = Literal["web", "lightroom", "photoshop", "api", "affinity", "gimp"]


def get_client_ip(request: Request) -> str:
    """Extract client IP from request, handling proxies."""
    # Check X-Forwarded-For header (when behind proxy/load balancer)
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    # Check X-Real-IP header
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    # Fall back to direct client IP
    return request.client.host if request.client else "unknown"


def get_user_agent(request: Request) -> str:
    """Extract user agent from request."""
    return request.headers.get("User-Agent", "")[:512]  # Limit to 512 chars


def detect_login_source(request: Request) -> LoginSource:
    """
    Detect the login source from request headers/user agent.
    Desktop plugins send specific User-Agent headers.
    """
    user_agent = request.headers.get("User-Agent", "").lower()
    
    # Check for plugin-specific user agents
    if "photomarklightroomplugin" in user_agent:
        return "lightroom"
    if "photomarkphotoshopplugin" in user_agent:
        return "photoshop"
    if "photomarkaffinityplugin" in user_agent:
        return "affinity"
    if "photomarkgimpplugin" in user_agent:
        return "gimp"
    
    # Check for custom header (plugins can also send this)
    source_header = request.headers.get("X-Photomark-Source", "").lower()
    if source_header in ("lightroom", "photoshop", "affinity", "gimp", "api"):
        return source_header
    
    # Check if it's an API token request (Bearer token starting with pm_)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer pm_"):
        return "api"
    
    return "web"


async def get_ip_location(ip: str) -> dict:
    """Get city, country, and country code from IP address using ip-api.com."""
    try:
        # Skip geolocation for localhost/private IPs
        if ip in ("127.0.0.1", "localhost", "unknown") or ip.startswith(("192.168.", "10.", "172.")):
            return {"city": "Local Network", "country": "Local", "country_code": None}
        
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://ip-api.com/json/{ip}?fields=status,city,country,countryCode")
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success":
                    return {
                        "city": data.get("city", "Unknown"),
                        "country": data.get("country", "Unknown"),
                        "country_code": data.get("countryCode"),
                    }
    except Exception as ex:
        logger.warning(f"IP geolocation failed for {ip}: {ex}")
    return {"city": "Unknown", "country": "Unknown", "country_code": None}


async def track_login_event(
    request: Request,
    uid: Optional[str] = None,
    success: bool = True,
    failure_reason: Optional[str] = None,
    attempted_email: Optional[str] = None,
    source: Optional[LoginSource] = None,
    update_last_login: bool = True,
) -> None:
    """
    Track a login event (success or failure).
    
    Args:
        request: FastAPI request object
        uid: User ID (required for successful logins, optional for failures)
        success: Whether the login was successful
        failure_reason: Reason for failure (e.g., "invalid_password", "account_locked")
        attempted_email: Email used in failed login attempt
        source: Login source override (auto-detected if not provided)
        update_last_login: Whether to update last_login_at on users table (for successful logins)
    """
    ip_address = get_client_ip(request)
    user_agent = get_user_agent(request)
    detected_source = source or detect_login_source(request)
    
    async def _store_login():
        try:
            # Get location info
            location = await get_ip_location(ip_address)
            
            db = SessionLocal()
            try:
                # Create login history record
                login_record = LoginHistory(
                    uid=uid,
                    ip_address=ip_address,
                    city=location["city"],
                    country=location["country"],
                    country_code=location.get("country_code"),
                    user_agent=user_agent,
                    source=detected_source,
                    success=success,
                    failure_reason=failure_reason if not success else None,
                    attempted_email=attempted_email if not success else None,
                )
                db.add(login_record)
                
                # Update last_login_at for successful logins
                if success and uid and update_last_login:
                    user = db.query(User).filter(User.uid == uid).first()
                    if user:
                        user.last_login_at = datetime.utcnow()
                        user.last_login_ip = ip_address
                
                db.commit()
                logger.info(f"[login_tracker] Stored {'successful' if success else 'failed'} login for {uid or attempted_email} from {ip_address} via {detected_source}")
            finally:
                db.close()
        except Exception as ex:
            logger.warning(f"[login_tracker] Failed to store login event: {ex}")
    
    # Run in background to not block the response
    asyncio.create_task(_store_login())


def track_login_event_sync(
    ip_address: str,
    user_agent: str,
    source: LoginSource,
    uid: Optional[str] = None,
    success: bool = True,
    failure_reason: Optional[str] = None,
    attempted_email: Optional[str] = None,
    update_last_login: bool = True,
) -> None:
    """
    Synchronous version for use in non-async contexts.
    Stores login event directly without background task.
    """
    try:
        db = SessionLocal()
        try:
            # Create login history record (without location - can be enriched later)
            login_record = LoginHistory(
                uid=uid,
                ip_address=ip_address,
                user_agent=user_agent,
                source=source,
                success=success,
                failure_reason=failure_reason if not success else None,
                attempted_email=attempted_email if not success else None,
            )
            db.add(login_record)
            
            # Update last_login_at for successful logins
            if success and uid and update_last_login:
                user = db.query(User).filter(User.uid == uid).first()
                if user:
                    user.last_login_at = datetime.utcnow()
                    user.last_login_ip = ip_address
            
            db.commit()
            logger.info(f"[login_tracker] Stored {'successful' if success else 'failed'} login for {uid or attempted_email} from {ip_address} via {source}")
        finally:
            db.close()
    except Exception as ex:
        logger.warning(f"[login_tracker] Failed to store login event: {ex}")
