"""
Admin Visitors Tracking API
Real-time visitor analytics for admin users
"""

import os
import json
import hashlib
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel
import httpx

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Admin emails allowed to access visitor data
ADMIN_EMAILS = [e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "dev.esstafasoufiane@protonmail.com").split(",")]

# In-memory visitor storage (use Redis in production)
visitors_store: Dict[str, Dict[str, Any]] = {}
visitor_sessions: Dict[str, datetime] = {}

# Session timeout (5 minutes = online)
SESSION_TIMEOUT = timedelta(minutes=5)


class VisitorData(BaseModel):
    page: str
    referrer: Optional[str] = None
    userAgent: Optional[str] = None


class Visitor(BaseModel):
    id: str
    ip: str
    country: str
    countryCode: str
    city: str
    region: str
    lat: float
    lng: float
    userAgent: str
    browser: str
    os: str
    device: str
    page: str
    referrer: str
    timestamp: str
    sessionId: str
    isOnline: bool


def get_admin_user(request: Request) -> str:
    """Verify admin access from Firebase token"""
    from core.auth import get_uid_from_request, get_user_email_from_uid
    
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Missing or invalid authorization")
    
    email = get_user_email_from_uid(uid)
    if not email:
        raise HTTPException(status_code=401, detail="Could not get user email")
    
    email = email.lower()
    if email not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    return email


def parse_user_agent(ua: str) -> Dict[str, str]:
    """Parse user agent string to extract browser, OS, and device"""
    browser = "Unknown"
    os_name = "Unknown"
    device = "Desktop"
    
    ua_lower = ua.lower()
    
    # Browser detection
    if "chrome" in ua_lower and "edg" not in ua_lower:
        browser = "Chrome"
    elif "firefox" in ua_lower:
        browser = "Firefox"
    elif "safari" in ua_lower and "chrome" not in ua_lower:
        browser = "Safari"
    elif "edg" in ua_lower:
        browser = "Edge"
    elif "opera" in ua_lower or "opr" in ua_lower:
        browser = "Opera"
    
    # OS detection
    if "windows" in ua_lower:
        os_name = "Windows"
    elif "mac os" in ua_lower or "macos" in ua_lower:
        os_name = "macOS"
    elif "linux" in ua_lower and "android" not in ua_lower:
        os_name = "Linux"
    elif "android" in ua_lower:
        os_name = "Android"
    elif "iphone" in ua_lower or "ipad" in ua_lower:
        os_name = "iOS"
    
    # Device detection
    if "mobile" in ua_lower or "android" in ua_lower or "iphone" in ua_lower:
        device = "Mobile"
    elif "tablet" in ua_lower or "ipad" in ua_lower:
        device = "Tablet"
    
    return {"browser": browser, "os": os_name, "device": device}


async def get_geo_from_ip(ip: str) -> Dict[str, Any]:
    """Get geolocation data from IP address using ip-api.com (free)"""
    try:
        # Skip localhost/private IPs
        if ip in ["127.0.0.1", "localhost", "::1"] or ip.startswith("192.168.") or ip.startswith("10."):
            return {
                "country": "Local",
                "countryCode": "XX",
                "city": "Localhost",
                "region": "Local",
                "lat": 0,
                "lng": 0
            }
        
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,city,regionName,lat,lon")
            data = response.json()
            
            if data.get("status") == "success":
                return {
                    "country": data.get("country", "Unknown"),
                    "countryCode": data.get("countryCode", "XX"),
                    "city": data.get("city", "Unknown"),
                    "region": data.get("regionName", "Unknown"),
                    "lat": data.get("lat", 0),
                    "lng": data.get("lon", 0)
                }
    except Exception as e:
        print(f"Geo lookup failed for {ip}: {e}")
    
    return {
        "country": "Unknown",
        "countryCode": "XX",
        "city": "Unknown",
        "region": "Unknown",
        "lat": 0,
        "lng": 0
    }


def generate_session_id(ip: str, user_agent: str) -> str:
    """Generate a unique session ID based on IP and user agent"""
    data = f"{ip}:{user_agent}:{datetime.utcnow().strftime('%Y-%m-%d')}"
    return hashlib.md5(data.encode()).hexdigest()[:16]


@router.get("/visitors/check")
async def check_admin_access(request: Request, admin_email: str = Depends(get_admin_user)):
    """Check if user has admin access (returns 200 if authorized, 403 if not)"""
    return {"ok": True, "email": admin_email}


@router.post("/track")
async def track_visitor(request: Request, data: VisitorData):
    """Track a visitor (called from frontend)"""
    # Get client IP
    ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
    if "," in ip:
        ip = ip.split(",")[0].strip()
    
    user_agent = data.userAgent or request.headers.get("User-Agent", "")
    session_id = generate_session_id(ip, user_agent)
    
    # Get geolocation
    geo = await get_geo_from_ip(ip)
    
    # Parse user agent
    ua_info = parse_user_agent(user_agent)
    
    # Create visitor record
    visitor_id = f"{session_id}_{datetime.utcnow().strftime('%H%M%S')}"
    visitor = {
        "id": visitor_id,
        "ip": ip,
        "country": geo["country"],
        "countryCode": geo["countryCode"],
        "city": geo["city"],
        "region": geo["region"],
        "lat": geo["lat"],
        "lng": geo["lng"],
        "userAgent": user_agent,
        "browser": ua_info["browser"],
        "os": ua_info["os"],
        "device": ua_info["device"],
        "page": data.page,
        "referrer": data.referrer or "direct",
        "timestamp": datetime.utcnow().isoformat(),
        "sessionId": session_id
    }
    
    # Store visitor
    visitors_store[visitor_id] = visitor
    visitor_sessions[session_id] = datetime.utcnow()
    
    # Clean up old visitors (keep last 24 hours)
    cutoff = datetime.utcnow() - timedelta(hours=24)
    to_remove = [k for k, v in visitors_store.items() if datetime.fromisoformat(v["timestamp"]) < cutoff]
    for k in to_remove:
        del visitors_store[k]
    
    return {"ok": True, "sessionId": session_id}


@router.get("/visitors")
async def get_visitors(request: Request, admin_email: str = Depends(get_admin_user)):
    """Get all visitors (admin only)"""
    now = datetime.utcnow()
    
    # Build visitor list with online status
    visitors = []
    for visitor in visitors_store.values():
        session_id = visitor.get("sessionId")
        last_seen = visitor_sessions.get(session_id)
        is_online = last_seen and (now - last_seen) < SESSION_TIMEOUT
        
        visitors.append({
            **visitor,
            "isOnline": is_online
        })
    
    # Sort by timestamp (newest first)
    visitors.sort(key=lambda x: x["timestamp"], reverse=True)
    
    # Calculate stats
    online_count = sum(1 for v in visitors if v["isOnline"])
    country_counts: Dict[str, Dict[str, Any]] = {}
    page_counts: Dict[str, int] = {}
    
    for v in visitors:
        code = v["countryCode"]
        if code not in country_counts:
            country_counts[code] = {"country": v["country"], "code": code, "count": 0}
        country_counts[code]["count"] += 1
        
        page = v["page"]
        page_counts[page] = page_counts.get(page, 0) + 1
    
    top_countries = sorted(country_counts.values(), key=lambda x: x["count"], reverse=True)
    top_pages = [{"page": k, "count": v} for k, v in sorted(page_counts.items(), key=lambda x: x[1], reverse=True)]
    
    stats = {
        "totalVisitors": len(visitors),
        "onlineNow": online_count,
        "uniqueCountries": len(country_counts),
        "topCountries": top_countries[:10],
        "topPages": top_pages[:10],
        "hourlyVisits": []  # Could be calculated from timestamps
    }
    
    return {
        "visitors": visitors[:100],  # Limit to 100 most recent
        "stats": stats
    }


@router.post("/heartbeat")
async def visitor_heartbeat(request: Request):
    """Update visitor session (keep-alive)"""
    ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
    if "," in ip:
        ip = ip.split(",")[0].strip()
    
    user_agent = request.headers.get("User-Agent", "")
    session_id = generate_session_id(ip, user_agent)
    
    if session_id in visitor_sessions:
        visitor_sessions[session_id] = datetime.utcnow()
        return {"ok": True, "sessionId": session_id}
    
    return {"ok": False, "message": "Session not found"}
