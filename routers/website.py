"""
Website Builder Settings Router
Handles persistence for the Website page builder (similar to Squarespace)
Stores websites per user in Neon PostgreSQL database
Includes custom domain management with DNS verification and SSL flow
"""
import json
import uuid
import re
import httpx
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Body
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import text

from core.config import logger
from core.auth import get_uid_from_request
from core.database import get_db
from models.website_domain import WebsiteDomain

router = APIRouter(prefix="/api/website", tags=["website"])


def _ensure_websites_table(db: Session):
    """Create websites table if it doesn't exist."""
    db.execute(text(
        """
        CREATE TABLE IF NOT EXISTS user_websites (
            id VARCHAR(64) PRIMARY KEY,
            uid VARCHAR(64) NOT NULL,
            name VARCHAR(255) NOT NULL DEFAULT 'My Website',
            slug VARCHAR(255),
            data JSONB NOT NULL DEFAULT '{}',
            is_published BOOLEAN DEFAULT FALSE,
            published_url VARCHAR(512),
            thumbnail_url VARCHAR(512),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_user_websites_uid ON user_websites(uid);
        """
    ))
    db.commit()


def _get_user_websites(db: Session, uid: str) -> list:
    """Get all websites for a user."""
    _ensure_websites_table(db)
    rows = db.execute(
        text("""
            SELECT id, name, slug, is_published, published_url, thumbnail_url, created_at, updated_at,
                   data->>'currentPageId' as current_page_id,
                   jsonb_array_length(COALESCE(data->'pages', '[]'::jsonb)) as page_count
            FROM user_websites 
            WHERE uid = :uid 
            ORDER BY updated_at DESC
        """),
        {"uid": uid}
    ).mappings().all()
    
    return [dict(row) for row in rows]


def _get_website(db: Session, uid: str, website_id: str) -> dict | None:
    """Get a specific website for a user."""
    _ensure_websites_table(db)
    row = db.execute(
        text("SELECT * FROM user_websites WHERE id = :id AND uid = :uid"),
        {"id": website_id, "uid": uid}
    ).mappings().first()
    
    if row:
        result = dict(row)
        data = result.get("data")
        if isinstance(data, str):
            try:
                result["data"] = json.loads(data)
            except Exception:
                result["data"] = {}
        return result
    return None


def _save_website(db: Session, uid: str, website_id: str | None, data: dict, name: str = None) -> str:
    """Save or create a website for a user."""
    _ensure_websites_table(db)
    
    # Generate new ID if not provided
    if not website_id:
        website_id = f"web_{uuid.uuid4().hex[:12]}"
    
    # Extract name from data if not provided
    if not name:
        pages = data.get("pages", [])
        if pages:
            name = f"Website ({len(pages)} pages)"
        else:
            name = "My Website"
    
    data_json = json.dumps(data) if isinstance(data, dict) else '{}'
    
    db.execute(text(
        """
        INSERT INTO user_websites (id, uid, name, data, updated_at)
        VALUES (:id, :uid, :name, CAST(:data_json AS jsonb), NOW())
        ON CONFLICT (id) DO UPDATE SET
            name = COALESCE(EXCLUDED.name, user_websites.name),
            data = EXCLUDED.data,
            updated_at = NOW();
        """
    ), {"id": website_id, "uid": uid, "name": name, "data_json": data_json})
    db.commit()
    
    return website_id


def _delete_website(db: Session, uid: str, website_id: str) -> bool:
    """Delete a website for a user."""
    _ensure_websites_table(db)
    result = db.execute(
        text("DELETE FROM user_websites WHERE id = :id AND uid = :uid"),
        {"id": website_id, "uid": uid}
    )
    db.commit()
    return result.rowcount > 0


# Legacy endpoint for backward compatibility
@router.get("/settings")
async def get_settings(request: Request, db: Session = Depends(get_db)):
    """Get the most recent website settings for the authenticated user."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        websites = _get_user_websites(db, uid)
        if websites:
            # Return the most recently updated website
            website = _get_website(db, uid, websites[0]["id"])
            if website:
                return {"ok": True, "data": website.get("data"), "websiteId": website["id"]}
        return {"ok": True, "data": None}
    except Exception as ex:
        logger.warning(f"get_website_settings failed: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


# Legacy endpoint for backward compatibility
@router.post("/settings")
async def save_settings(request: Request, payload: dict, db: Session = Depends(get_db)):
    """Save website settings for the authenticated user."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        data = payload.get("data")
        if not isinstance(data, dict):
            return JSONResponse({"error": "Invalid data format"}, status_code=400)
        
        website_id = payload.get("websiteId")
        name = payload.get("name")
        
        # If no website_id, check if user has any websites
        if not website_id:
            websites = _get_user_websites(db, uid)
            if websites:
                website_id = websites[0]["id"]
        
        saved_id = _save_website(db, uid, website_id, data, name)
        return {"ok": True, "websiteId": saved_id}
    except Exception as ex:
        logger.warning(f"save_website_settings failed: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


# New endpoints for multiple websites
@router.get("/list")
async def list_websites(request: Request, db: Session = Depends(get_db)):
    """List all websites for the authenticated user."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        websites = _get_user_websites(db, uid)
        return {"ok": True, "websites": websites}
    except Exception as ex:
        logger.warning(f"list_websites failed: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.get("/{website_id}")
async def get_website_by_id(website_id: str, request: Request, db: Session = Depends(get_db)):
    """Get a specific website by ID."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        website = _get_website(db, uid, website_id)
        if not website:
            return JSONResponse({"error": "Website not found"}, status_code=404)
        return {"ok": True, "website": website}
    except Exception as ex:
        logger.warning(f"get_website failed: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.post("/create")
async def create_website(request: Request, payload: dict, db: Session = Depends(get_db)):
    """Create a new website."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        name = payload.get("name", "My Website")
        data = payload.get("data", {"pages": [], "currentPageId": ""})
        
        website_id = _save_website(db, uid, None, data, name)
        return {"ok": True, "websiteId": website_id}
    except Exception as ex:
        logger.warning(f"create_website failed: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.put("/{website_id}")
async def update_website(website_id: str, request: Request, payload: dict, db: Session = Depends(get_db)):
    """Update a specific website."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        # Verify ownership
        existing = _get_website(db, uid, website_id)
        if not existing:
            return JSONResponse({"error": "Website not found"}, status_code=404)
        
        data = payload.get("data")
        name = payload.get("name")
        
        if data is not None:
            _save_website(db, uid, website_id, data, name)
        
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"update_website failed: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.delete("/{website_id}")
async def delete_website(website_id: str, request: Request, db: Session = Depends(get_db)):
    """Delete a website."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        deleted = _delete_website(db, uid, website_id)
        if not deleted:
            return JSONResponse({"error": "Website not found"}, status_code=404)
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"delete_website failed: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


# ============================================================================
# Custom Domain Management (same flow as uploads and shop domains)
# ============================================================================

def _normalize_domain(dom: str | None) -> str | None:
    """Normalize domain hostname"""
    if not dom:
        return None
    return dom.strip().lower().rstrip('.')


async def _check_domain_dns(hostname: str, db: Session = None, uid: str = None, website_id: str = None) -> dict:
    """Check DNS CNAME and TLS status using Cloudflare DNS over HTTPS.
    
    If db, uid, and website_id are provided, updates dnsVerified in database immediately
    so Caddy can issue SSL certificate on the next request.
    """
    dns_verified = False
    cname_target = None
    ssl_error = None
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"https://cloudflare-dns.com/dns-query?name={hostname}&type=CNAME",
                headers={"Accept": "application/dns-json"}
            )
            data = r.json()
            answers = data.get("Answer") or []
            for ans in answers:
                if (ans.get("type") == 5) and ans.get("data"):
                    cname_target = (ans["data"] or "").strip(".").lower()
                    if cname_target == "api.photomark.cloud":
                        dns_verified = True
                        break
    except Exception as e:
        logger.warning(f"DNS check failed for {hostname}: {e}")
        dns_verified = False
    
    # If DNS is verified and we have db access, update immediately
    # This allows Caddy to issue SSL certificate on the SSL check request
    if dns_verified and db and uid and website_id:
        try:
            domain = db.query(WebsiteDomain).filter(
                WebsiteDomain.uid == uid,
                WebsiteDomain.website_id == website_id
            ).first()
            if domain and not domain.dns_verified:
                domain.dns_verified = True
                domain.cname_observed = cname_target
                domain.last_checked = datetime.utcnow()
                db.commit()
                logger.info(f"DNS verified for {hostname}, updated database for SSL issuance")
        except Exception as e:
            logger.warning(f"Failed to update dnsVerified early: {e}")
            try:
                db.rollback()
            except:
                pass
    
    ssl_status = "unknown"
    if dns_verified:
        try:
            # Use longer timeout for SSL check as Caddy may need to issue certificate
            async with httpx.AsyncClient(timeout=10.0, verify=True) as client:
                h = await client.get(f"https://{hostname}", follow_redirects=True)
                # Any successful HTTPS connection means SSL is working
                if h.status_code < 500:
                    ssl_status = "active"
                    logger.info(f"SSL check for {hostname}: status={h.status_code}, ssl=active")
                else:
                    ssl_status = "pending"
        except Exception as e:
            ssl_error = str(e)
            err_str = str(e).lower()
            if "certificate" in err_str or "ssl" in err_str or "tls" in err_str:
                ssl_status = "pending"
                logger.info(f"SSL check for {hostname}: certificate error - {ssl_error}")
            else:
                ssl_status = "pending"
                logger.info(f"SSL check for {hostname}: error - {ssl_error}")
    else:
        ssl_status = "blocked"
    
    return {
        "dnsVerified": dns_verified,
        "sslStatus": ssl_status,
        "cnameObserved": cname_target,
        "error": ssl_error,
    }


@router.get("/domain/config")
async def get_website_domain_config(
    request: Request,
    website_id: str = None,
    db: Session = Depends(get_db)
):
    """Get current website domain configuration for the authenticated user"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        # If website_id provided, get domain for that website
        if website_id:
            domain = db.query(WebsiteDomain).filter(
                WebsiteDomain.uid == uid,
                WebsiteDomain.website_id == website_id
            ).first()
        else:
            # Get the first domain for this user
            domain = db.query(WebsiteDomain).filter(WebsiteDomain.uid == uid).first()
        
        if not domain:
            return {"domain": {}}
        
        return {"domain": domain.to_dict()}
    except Exception as e:
        logger.error(f"Failed to get domain config for {uid}: {e}")
        return JSONResponse({"error": f"Failed to get domain config: {str(e)}"}, status_code=500)


@router.post("/domain")
async def set_website_domain(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db)
):
    """Configure a custom domain for a user's website"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    hostname = _normalize_domain(payload.get('hostname'))
    website_id = payload.get('websiteId')
    
    if not hostname:
        return JSONResponse({"error": "hostname is required"}, status_code=400)

    # Basic hostname validation
    if len(hostname) > 255 or not re.match(r'^[a-z0-9][a-z0-9.-]*[a-z0-9]$', hostname):
        return JSONResponse({"error": "invalid hostname"}, status_code=400)

    try:
        # If no website_id, get the user's first website
        if not website_id:
            websites = _get_user_websites(db, uid)
            if websites:
                website_id = websites[0]["id"]
            else:
                return JSONResponse({"error": "No website found. Create a website first."}, status_code=404)
        
        # Verify website ownership
        website = _get_website(db, uid, website_id)
        if not website:
            return JSONResponse({"error": "Website not found"}, status_code=404)
        
        # Check if user already has a domain configured for this website
        existing = db.query(WebsiteDomain).filter(
            WebsiteDomain.uid == uid,
            WebsiteDomain.website_id == website_id
        ).first()
        
        if existing:
            if existing.hostname != hostname:
                return JSONResponse({"error": "domain_already_set"}, status_code=409)
            # Same hostname, just return current config
            return {
                "success": True,
                "message": "Domain already configured.",
                "instructions": {
                    "recordType": "CNAME",
                    "name": hostname,
                    "value": "api.photomark.cloud",
                    "ttl": 300
                },
                "domain": existing.to_dict()
            }
        
        # Check if hostname is already taken by another user
        hostname_taken = db.query(WebsiteDomain).filter(WebsiteDomain.hostname == hostname).first()
        if hostname_taken:
            return JSONResponse({"error": "hostname_already_taken"}, status_code=409)
        
        # Create new domain record
        domain = WebsiteDomain(
            id=str(uuid.uuid4()),
            uid=uid,
            website_id=website_id,
            hostname=hostname,
            dns_verified=False,
            ssl_status='unknown',
            enabled=False
        )
        db.add(domain)
        db.commit()
        db.refresh(domain)
        
        logger.info(f"Created website domain {hostname} for user {uid[:8]}... website {website_id}")

        return {
            "success": True,
            "message": "Custom domain saved. Create the CNAME record and check status.",
            "instructions": {
                "recordType": "CNAME",
                "name": hostname,
                "value": "api.photomark.cloud",
                "ttl": 300
            },
            "domain": domain.to_dict()
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to set custom domain for {uid}: {e}")
        return JSONResponse({"error": f"Failed to set custom domain: {str(e)}"}, status_code=500)


@router.post("/domain/remove")
async def remove_website_domain(
    request: Request,
    payload: dict = Body(default={}),
    db: Session = Depends(get_db)
):
    """Remove custom domain from website"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    website_id = payload.get('websiteId') if payload else None
    
    try:
        # Build query
        query = db.query(WebsiteDomain).filter(WebsiteDomain.uid == uid)
        if website_id:
            query = query.filter(WebsiteDomain.website_id == website_id)
        
        domain = query.first()
        
        if domain:
            hostname = domain.hostname
            db.delete(domain)
            db.commit()
            logger.info(f"Removed website domain {hostname} for user {uid[:8]}...")
        
        return {"ok": True}
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to remove domain for {uid}: {e}")
        return JSONResponse({"error": f"Failed to remove domain: {str(e)}"}, status_code=500)


@router.post("/domain/enable")
async def enable_website_domain(
    request: Request,
    payload: dict = Body(default={}),
    db: Session = Depends(get_db)
):
    """Enable custom domain for website (after DNS verified)"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    website_id = payload.get('websiteId') if payload else None
    
    try:
        # Build query
        query = db.query(WebsiteDomain).filter(WebsiteDomain.uid == uid)
        if website_id:
            query = query.filter(WebsiteDomain.website_id == website_id)
        
        domain = query.first()
        
        if not domain:
            return JSONResponse({"error": "No domain configured"}, status_code=404)
        
        logger.info(f"Enable domain {domain.hostname}: dnsVerified={domain.dns_verified}, sslStatus={domain.ssl_status}")
        
        if not domain.dns_verified:
            return JSONResponse({"error": f"domain_not_ready: dns=False"}, status_code=412)
        
        if domain.ssl_status != 'active':
            return JSONResponse({"error": f"domain_not_ready: ssl={domain.ssl_status}"}, status_code=412)
        
        domain.enabled = True
        domain.updated_at = datetime.utcnow()
        db.commit()
        
        logger.info(f"Domain {domain.hostname} enabled successfully for user {uid[:8]}...")
        return {"ok": True, "domain": domain.to_dict()}
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to enable domain for {uid}: {e}")
        return JSONResponse({"error": f"Failed to enable domain: {str(e)}"}, status_code=500)


@router.get("/domain/status")
async def get_website_domain_status(
    request: Request,
    hostname: str = None,
    website_id: str = None,
    db: Session = Depends(get_db)
):
    """Check DNS CNAME and TLS status for website custom domain"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        # Build query
        query = db.query(WebsiteDomain).filter(WebsiteDomain.uid == uid)
        if website_id:
            query = query.filter(WebsiteDomain.website_id == website_id)
        
        domain = query.first()
        
        # Use provided hostname or get from database
        hostname = _normalize_domain(hostname)
        if not hostname and domain:
            hostname = domain.hostname
        
        if not hostname:
            return JSONResponse({"error": "No hostname configured"}, status_code=400)
        
        # Security check: if domain exists, ensure it belongs to this user
        if domain and domain.hostname != hostname:
            return JSONResponse({"error": "Hostname mismatch"}, status_code=403)

        # Get website_id from domain if not provided
        if not website_id and domain:
            website_id = domain.website_id

        # Check DNS status (pass db and uid so dnsVerified can be saved early for SSL)
        status = await _check_domain_dns(hostname, db=db, uid=uid, website_id=website_id)
        
        # Update or create domain record
        if not domain:
            # Need website_id to create domain
            if not website_id:
                websites = _get_user_websites(db, uid)
                if websites:
                    website_id = websites[0]["id"]
                else:
                    return JSONResponse({"error": "No website found"}, status_code=404)
            
            domain = WebsiteDomain(
                id=str(uuid.uuid4()),
                uid=uid,
                website_id=website_id,
                hostname=hostname,
                dns_verified=status['dnsVerified'],
                ssl_status=status['sslStatus'],
                cname_observed=status['cnameObserved'],
                last_error=status.get('error'),
                last_checked=datetime.utcnow(),
                enabled=False
            )
            db.add(domain)
        else:
            domain.dns_verified = status['dnsVerified']
            domain.ssl_status = status['sslStatus']
            domain.cname_observed = status['cnameObserved']
            domain.last_error = status.get('error')
            domain.last_checked = datetime.utcnow()
        
        db.commit()
        db.refresh(domain)
        
        logger.info(f"Saved domain status for {hostname}: dnsVerified={status['dnsVerified']}, sslStatus={status['sslStatus']}")

        return {
            "hostname": hostname,
            "websiteId": domain.website_id,
            "dnsVerified": status['dnsVerified'],
            "sslStatus": status['sslStatus'],
            "cnameObserved": status['cnameObserved'],
            "enabled": domain.enabled,
            "instructions": {
                "recordType": "CNAME",
                "name": hostname,
                "value": "api.photomark.cloud",
                "ttl": 300
            }
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to check domain status for {uid}: {e}")
        return JSONResponse({"error": f"Failed to check domain status: {str(e)}"}, status_code=500)
