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
signups_store: Dict[str, Dict[str, Any]] = {}  # Track signups
activity_store: Dict[str, List[Dict[str, Any]]] = {}  # Track visitor activities by session

# Session timeout (3 minutes = online, after that marked as offline)
SESSION_TIMEOUT = timedelta(minutes=3)
# Max activities per session
MAX_ACTIVITIES_PER_SESSION = 100


class VisitorData(BaseModel):
    page: str
    referrer: Optional[str] = None
    userAgent: Optional[str] = None


class ActivityItem(BaseModel):
    sessionId: str
    type: str  # 'page_view', 'click', 'scroll', 'form_input', 'download', 'hover', 'copy', 'search'
    target: Optional[str] = None  # Button text, link URL, element ID
    page: str
    metadata: Optional[Dict[str, Any]] = None


class ActivityBatch(BaseModel):
    activities: List[ActivityItem]


class SignupData(BaseModel):
    email: str
    name: Optional[str] = None
    method: str  # 'email' or 'google'
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
    """Get geolocation data from IP address using IPinfo API"""
    try:
        # Skip localhost/private IPs
        if ip in ["127.0.0.1", "localhost", "::1"] or ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
            return {
                "country": "Local",
                "countryCode": "XX",
                "city": "Localhost",
                "region": "Local",
                "lat": 0,
                "lng": 0
            }
        
        # Get IPinfo token from environment (optional - works without token with rate limits)
        ipinfo_token = os.getenv("IPINFO_TOKEN", "")
        
        async with httpx.AsyncClient(timeout=5.0) as client:
            url = f"https://ipinfo.io/{ip}/json"
            if ipinfo_token:
                url += f"?token={ipinfo_token}"
            
            response = await client.get(url)
            data = response.json()
            
            # IPinfo returns loc as "lat,lng" string
            lat, lng = 0.0, 0.0
            if data.get("loc"):
                try:
                    lat_str, lng_str = data["loc"].split(",")
                    lat = float(lat_str)
                    lng = float(lng_str)
                except (ValueError, AttributeError):
                    pass
            
            # IPinfo uses 2-letter country codes
            country_code = data.get("country", "XX")
            
            # Map country code to full name (common ones)
            country_names = {
                "US": "United States", "GB": "United Kingdom", "CA": "Canada",
                "AU": "Australia", "DE": "Germany", "FR": "France", "JP": "Japan",
                "IN": "India", "BR": "Brazil", "MX": "Mexico", "ES": "Spain",
                "IT": "Italy", "NL": "Netherlands", "SE": "Sweden", "NO": "Norway",
                "DK": "Denmark", "FI": "Finland", "PL": "Poland", "RU": "Russia",
                "CN": "China", "KR": "South Korea", "SG": "Singapore", "HK": "Hong Kong",
                "TW": "Taiwan", "NZ": "New Zealand", "IE": "Ireland", "CH": "Switzerland",
                "AT": "Austria", "BE": "Belgium", "PT": "Portugal", "GR": "Greece",
                "CZ": "Czech Republic", "HU": "Hungary", "RO": "Romania", "UA": "Ukraine",
                "ZA": "South Africa", "EG": "Egypt", "NG": "Nigeria", "KE": "Kenya",
                "MA": "Morocco", "AR": "Argentina", "CL": "Chile", "CO": "Colombia",
                "PE": "Peru", "VE": "Venezuela", "TH": "Thailand", "VN": "Vietnam",
                "PH": "Philippines", "ID": "Indonesia", "MY": "Malaysia", "PK": "Pakistan",
                "BD": "Bangladesh", "TR": "Turkey", "SA": "Saudi Arabia", "AE": "UAE",
                "IL": "Israel", "QA": "Qatar", "KW": "Kuwait"
            }
            country_name = country_names.get(country_code, data.get("country", "Unknown"))
            
            return {
                "country": country_name,
                "countryCode": country_code,
                "city": data.get("city", "Unknown"),
                "region": data.get("region", "Unknown"),
                "lat": lat,
                "lng": lng
            }
    except Exception as e:
        print(f"IPinfo geo lookup failed for {ip}: {e}")
    
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
    
    # Get signups sorted by timestamp (newest first)
    signups = sorted(signups_store.values(), key=lambda x: x.get("timestamp", ""), reverse=True)
    
    return {
        "visitors": visitors[:100],  # Limit to 100 most recent
        "signups": signups[:50],  # Limit to 50 most recent signups
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


@router.post("/track-signup")
async def track_signup(request: Request, data: SignupData):
    """Track a new user signup (called from frontend after successful registration)"""
    # Get client IP
    ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
    if "," in ip:
        ip = ip.split(",")[0].strip()
    
    user_agent = data.userAgent or request.headers.get("User-Agent", "")
    
    # Get geolocation
    geo = await get_geo_from_ip(ip)
    
    # Parse user agent
    ua_info = parse_user_agent(user_agent)
    
    # Create signup record
    signup_id = f"signup_{hashlib.md5(data.email.encode()).hexdigest()[:12]}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    
    signup = {
        "id": signup_id,
        "email": data.email,
        "name": data.name or "",
        "method": data.method,  # 'email' or 'google'
        "ip": ip,
        "country": geo["country"],
        "countryCode": geo["countryCode"],
        "city": geo["city"],
        "browser": ua_info["browser"],
        "os": ua_info["os"],
        "device": ua_info["device"],
        "timestamp": datetime.utcnow().isoformat(),
    }
    
    # Store signup (use email as key to avoid duplicates)
    signups_store[data.email.lower()] = signup
    
    # Clean up old signups (keep last 7 days)
    cutoff = datetime.utcnow() - timedelta(days=7)
    to_remove = [k for k, v in signups_store.items() if datetime.fromisoformat(v["timestamp"]) < cutoff]
    for k in to_remove:
        del signups_store[k]
    
    return {"ok": True, "signupId": signup_id}


@router.post("/track-activity")
async def track_activity(request: Request, data: ActivityBatch):
    """Track visitor activities (clicks, scrolls, form inputs, etc.)"""
    for activity in data.activities:
        session_id = activity.sessionId
        
        # Initialize activity list for session if not exists
        if session_id not in activity_store:
            activity_store[session_id] = []
        
        # Add activity with timestamp
        activity_record = {
            "type": activity.type,
            "target": activity.target,
            "page": activity.page,
            "metadata": activity.metadata or {},
            "timestamp": datetime.utcnow().isoformat()
        }
        
        activity_store[session_id].append(activity_record)
        
        # Limit activities per session
        if len(activity_store[session_id]) > MAX_ACTIVITIES_PER_SESSION:
            activity_store[session_id] = activity_store[session_id][-MAX_ACTIVITIES_PER_SESSION:]
    
    # Clean up old activity sessions (keep last 2 hours)
    cutoff = datetime.utcnow() - timedelta(hours=2)
    sessions_to_remove = []
    for session_id, activities in activity_store.items():
        if activities:
            last_activity_time = datetime.fromisoformat(activities[-1]["timestamp"])
            if last_activity_time < cutoff:
                sessions_to_remove.append(session_id)
    
    for session_id in sessions_to_remove:
        del activity_store[session_id]
    
    return {"ok": True, "count": len(data.activities)}


@router.get("/activities/{session_id}")
async def get_session_activities(session_id: str, request: Request, admin_email: str = Depends(get_admin_user)):
    """Get activities for a specific session (admin only)"""
    activities = activity_store.get(session_id, [])
    return {"sessionId": session_id, "activities": activities, "count": len(activities)}


@router.get("/activities")
async def get_all_activities(request: Request, admin_email: str = Depends(get_admin_user)):
    """Get all recent activities grouped by session (admin only)"""
    now = datetime.utcnow()
    result = []
    
    for session_id, activities in activity_store.items():
        if not activities:
            continue
        
        # Get visitor info for this session
        visitor_info = None
        for visitor in visitors_store.values():
            if visitor.get("sessionId") == session_id:
                visitor_info = visitor
                break
        
        # Check if session is online
        last_seen = visitor_sessions.get(session_id)
        is_online = last_seen and (now - last_seen) < SESSION_TIMEOUT
        
        result.append({
            "sessionId": session_id,
            "visitor": visitor_info,
            "isOnline": is_online,
            "activities": activities[-50:],  # Last 50 activities
            "activityCount": len(activities),
            "lastActivity": activities[-1]["timestamp"] if activities else None
        })
    
    # Sort by last activity (most recent first)
    result.sort(key=lambda x: x.get("lastActivity") or "", reverse=True)
    
    return {"sessions": result[:50]}
