"""
Uploads Domain Router - Custom domain management for uploads preview page
"""
from fastapi import APIRouter, HTTPException, Request, Depends, Body
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from datetime import datetime
import re
import httpx

from core.auth import get_uid_from_request
from core.database import get_db
from core.config import logger
from models.user import User

router = APIRouter(prefix="/api/uploads", tags=["uploads-domain"])


def _normalize_domain(dom: str | None) -> str | None:
    if not dom:
        return None
    return dom.strip().lower().rstrip('.')


@router.get('/domain/config')
async def get_uploads_domain_config(request: Request, db: Session = Depends(get_db)):
    """Get current uploads domain configuration"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        user = db.query(User).filter(User.uid == uid).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Store uploads domain config in user's extra_metadata (JSON field)
        meta = user.extra_metadata or {}
        domain_config = meta.get('uploads_domain') or {}
        
        return {"domain": domain_config}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get domain config: {str(e)}")


@router.post('/domain')
async def set_uploads_domain(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db)
):
    """Configure a custom domain for the user's uploads preview page"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")

    hostname = _normalize_domain(payload.get('hostname'))
    if not hostname:
        raise HTTPException(status_code=400, detail="hostname is required")

    # Basic hostname validation
    if len(hostname) > 255 or not re.match(r'^[a-z0-9][a-z0-9.-]*[a-z0-9]$', hostname):
        raise HTTPException(status_code=400, detail="invalid hostname")

    try:
        user = db.query(User).filter(User.uid == uid).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        meta = user.extra_metadata or {}
        existing = meta.get('uploads_domain') or {}
        current_host = _normalize_domain(existing.get('hostname'))
        
        if current_host and current_host != hostname:
            raise HTTPException(status_code=409, detail="domain_already_set")

        now = datetime.utcnow().isoformat()
        domain_config = {
            "hostname": hostname,
            "dnsTarget": "api.photomark.cloud",
            "dnsVerified": False,
            "sslStatus": "unknown",
            "lastChecked": now,
            "enabled": False,
        }
        
        meta['uploads_domain'] = domain_config
        user.extra_metadata = meta
        user.updated_at = datetime.utcnow()
        db.commit()

        instructions = {
            "recordType": "CNAME",
            "name": hostname,
            "value": "api.photomark.cloud",
            "ttl": 300
        }

        return {
            "success": True,
            "message": "Custom domain saved. Create the CNAME record and check status.",
            "instructions": instructions,
            "domain": domain_config
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to set custom domain: {str(e)}")


@router.post('/domain/remove')
async def remove_uploads_domain(request: Request, db: Session = Depends(get_db)):
    """Remove custom domain from uploads"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        user = db.query(User).filter(User.uid == uid).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        meta = user.extra_metadata or {}
        meta['uploads_domain'] = {}
        user.extra_metadata = meta
        user.updated_at = datetime.utcnow()
        db.commit()
        
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to remove domain: {str(e)}")


@router.post('/domain/enable')
async def enable_uploads_domain(request: Request, db: Session = Depends(get_db)):
    """Enable custom domain for uploads (after DNS verified)"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        user = db.query(User).filter(User.uid == uid).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        meta = user.extra_metadata or {}
        domain_config = dict(meta.get('uploads_domain') or {})
        hostname = _normalize_domain(domain_config.get('hostname'))
        
        if not hostname:
            raise HTTPException(status_code=400, detail="No hostname configured")
        
        dns_ok = bool(domain_config.get('dnsVerified'))
        ssl_ok = str(domain_config.get('sslStatus') or '').strip().lower() == 'active'
        
        logger.info(f"Enable domain {hostname}: dnsVerified={dns_ok}, sslStatus={domain_config.get('sslStatus')}, ssl_ok={ssl_ok}")
        
        if not (dns_ok and ssl_ok):
            raise HTTPException(status_code=412, detail=f"domain_not_ready: dns={dns_ok}, ssl={ssl_ok}")
        
        from sqlalchemy.orm.attributes import flag_modified
        domain_config['enabled'] = True
        meta = dict(meta)
        meta['uploads_domain'] = domain_config
        user.extra_metadata = meta
        user.updated_at = datetime.utcnow()
        flag_modified(user, 'extra_metadata')
        db.commit()
        
        logger.info(f"Domain {hostname} enabled successfully")
        return {"ok": True, "domain": domain_config}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to enable domain: {str(e)}")


async def _check_domain_dns(hostname: str, db: Session = None, uid: str = None) -> dict:
    """Check DNS CNAME and TLS status using Cloudflare DNS over HTTPS.
    
    If db and uid are provided, updates dnsVerified in database immediately
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
    except Exception:
        dns_verified = False
    
    # If DNS is verified and we have db access, update immediately
    # This allows Caddy to issue SSL certificate on the SSL check request
    if dns_verified and db and uid:
        try:
            from sqlalchemy.orm.attributes import flag_modified
            user = db.query(User).filter(User.uid == uid).first()
            if user:
                meta = dict(user.extra_metadata or {})
                domain_config = dict(meta.get('uploads_domain') or {})
                domain_config['dnsVerified'] = True
                domain_config['cnameObserved'] = cname_target
                domain_config['hostname'] = hostname
                domain_config['lastChecked'] = datetime.utcnow().isoformat()
                meta['uploads_domain'] = domain_config
                user.extra_metadata = meta
                flag_modified(user, 'extra_metadata')
                db.commit()
                db.refresh(user)
                logger.info(f"DNS verified for {hostname}, updated database for SSL issuance. dnsVerified={domain_config.get('dnsVerified')}")
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
            # Use GET instead of HEAD as some routes don't support HEAD
            async with httpx.AsyncClient(timeout=10.0, verify=True) as client:
                h = await client.get(f"https://{hostname}", follow_redirects=True)
                # Any successful HTTPS connection means SSL is working
                # 405 Method Not Allowed still means SSL is active
                if h.status_code < 500:
                    ssl_status = "active"
                    logger.info(f"SSL check for {hostname}: status={h.status_code}, ssl=active")
                else:
                    ssl_status = "pending"
        except Exception as e:
            ssl_error = str(e)
            err_str = str(e).lower()
            # Check if it's a certificate error vs connection error
            if "certificate" in err_str or "ssl" in err_str or "tls" in err_str:
                ssl_status = "pending"  # Certificate not yet issued
                logger.info(f"SSL check for {hostname}: certificate error - {ssl_error}")
            else:
                # Connection succeeded but other error - SSL might be working
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


@router.get('/domain/status')
async def get_uploads_domain_status(
    request: Request,
    hostname: str | None = None,
    db: Session = Depends(get_db)
):
    """Check DNS CNAME and TLS status for uploads custom domain"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        user = db.query(User).filter(User.uid == uid).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        meta = user.extra_metadata or {}
        domain_config = meta.get('uploads_domain') or {}
        
        # Use provided hostname or get from config
        hostname = _normalize_domain(hostname) or _normalize_domain(domain_config.get('hostname'))
        
        if not hostname:
            raise HTTPException(status_code=400, detail="No hostname configured")

        # Check DNS status (pass db and uid so dnsVerified can be saved early for SSL)
        status = await _check_domain_dns(hostname, db=db, uid=uid)
        
        # Update stored status
        from sqlalchemy.orm.attributes import flag_modified
        domain_config = dict(domain_config)  # Ensure it's a new dict
        domain_config['hostname'] = hostname
        domain_config['dnsVerified'] = status['dnsVerified']
        domain_config['sslStatus'] = status['sslStatus']
        domain_config['cnameObserved'] = status['cnameObserved']
        domain_config['lastChecked'] = datetime.utcnow().isoformat()
        if status.get('error'):
            domain_config['lastError'] = status['error']
        
        meta = dict(meta)  # Ensure it's a new dict
        meta['uploads_domain'] = domain_config
        user.extra_metadata = meta
        user.updated_at = datetime.utcnow()
        flag_modified(user, 'extra_metadata')
        db.commit()
        
        logger.info(f"Saved domain status for {hostname}: dnsVerified={status['dnsVerified']}, sslStatus={status['sslStatus']}")

        instructions = {
            "recordType": "CNAME",
            "name": hostname,
            "value": "api.photomark.cloud",
            "ttl": 300
        }

        return {
            "hostname": hostname,
            "dnsVerified": status['dnsVerified'],
            "sslStatus": status['sslStatus'],
            "cnameObserved": status['cnameObserved'],
            "enabled": bool(domain_config.get('enabled')),
            "instructions": instructions
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to check domain status: {str(e)}")
