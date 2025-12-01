"""
Uploads Domain Router - Custom domain management for uploads preview page
Uses dedicated uploads_domains table for better security and performance
"""
from fastapi import APIRouter, HTTPException, Request, Depends, Body
from sqlalchemy.orm import Session
from datetime import datetime
import re
import httpx
import uuid

from core.auth import get_uid_from_request
from core.database import get_db
from core.config import logger
from models.uploads_domain import UploadsDomain

router = APIRouter(prefix="/api/uploads", tags=["uploads-domain"])


def _normalize_domain(dom: str | None) -> str | None:
    if not dom:
        return None
    return dom.strip().lower().rstrip('.')


@router.get('/domain/config')
async def get_uploads_domain_config(request: Request, db: Session = Depends(get_db)):
    """Get current uploads domain configuration for the authenticated user"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        # Query by user UID - ensures user can only see their own domain
        domain = db.query(UploadsDomain).filter(UploadsDomain.uid == uid).first()
        
        if not domain:
            return {"domain": {}}
        
        return {"domain": domain.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get domain config for {uid}: {e}")
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
        # Check if user already has a domain configured
        existing = db.query(UploadsDomain).filter(UploadsDomain.uid == uid).first()
        
        if existing:
            if existing.hostname != hostname:
                raise HTTPException(status_code=409, detail="domain_already_set")
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
        hostname_taken = db.query(UploadsDomain).filter(UploadsDomain.hostname == hostname).first()
        if hostname_taken:
            raise HTTPException(status_code=409, detail="hostname_already_taken")
        
        # Create new domain record
        domain = UploadsDomain(
            id=str(uuid.uuid4()),
            uid=uid,
            hostname=hostname,
            dns_verified=False,
            ssl_status='unknown',
            enabled=False
        )
        db.add(domain)
        db.commit()
        db.refresh(domain)
        
        logger.info(f"Created uploads domain {hostname} for user {uid[:8]}...")

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
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to set custom domain for {uid}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to set custom domain: {str(e)}")


@router.post('/domain/remove')
async def remove_uploads_domain(request: Request, db: Session = Depends(get_db)):
    """Remove custom domain from uploads"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        # Only delete domain owned by this user
        domain = db.query(UploadsDomain).filter(UploadsDomain.uid == uid).first()
        
        if domain:
            hostname = domain.hostname
            db.delete(domain)
            db.commit()
            logger.info(f"Removed uploads domain {hostname} for user {uid[:8]}...")
        
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to remove domain for {uid}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to remove domain: {str(e)}")


@router.post('/domain/enable')
async def enable_uploads_domain(request: Request, db: Session = Depends(get_db)):
    """Enable custom domain for uploads (after DNS verified)"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        # Only enable domain owned by this user
        domain = db.query(UploadsDomain).filter(UploadsDomain.uid == uid).first()
        
        if not domain:
            raise HTTPException(status_code=404, detail="No domain configured")
        
        logger.info(f"Enable domain {domain.hostname}: dnsVerified={domain.dns_verified}, sslStatus={domain.ssl_status}")
        
        if not domain.dns_verified:
            raise HTTPException(status_code=412, detail=f"domain_not_ready: dns=False")
        
        if domain.ssl_status != 'active':
            raise HTTPException(status_code=412, detail=f"domain_not_ready: ssl={domain.ssl_status}")
        
        domain.enabled = True
        domain.updated_at = datetime.utcnow()
        db.commit()
        
        logger.info(f"Domain {domain.hostname} enabled successfully for user {uid[:8]}...")
        return {"ok": True, "domain": domain.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to enable domain for {uid}: {e}")
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
    except Exception as e:
        logger.warning(f"DNS check failed for {hostname}: {e}")
        dns_verified = False
    
    # If DNS is verified and we have db access, update immediately
    # This allows Caddy to issue SSL certificate on the SSL check request
    if dns_verified and db and uid:
        try:
            domain = db.query(UploadsDomain).filter(UploadsDomain.uid == uid).first()
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
        # Get domain owned by this user
        domain = db.query(UploadsDomain).filter(UploadsDomain.uid == uid).first()
        
        # Use provided hostname or get from database
        hostname = _normalize_domain(hostname)
        if not hostname and domain:
            hostname = domain.hostname
        
        if not hostname:
            raise HTTPException(status_code=400, detail="No hostname configured")
        
        # Security check: if domain exists, ensure it belongs to this user
        if domain and domain.hostname != hostname:
            raise HTTPException(status_code=403, detail="Hostname mismatch")

        # Check DNS status (pass db and uid so dnsVerified can be saved early for SSL)
        status = await _check_domain_dns(hostname, db=db, uid=uid)
        
        # Update or create domain record
        if not domain:
            domain = UploadsDomain(
                id=str(uuid.uuid4()),
                uid=uid,
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
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to check domain status for {uid}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to check domain status: {str(e)}")


@router.options('/public/{uid}')
async def options_public_uploads(request: Request, uid: str):
    """Handle CORS preflight for public uploads endpoint"""
    from fastapi.responses import Response
    origin = request.headers.get('origin', '*')
    return Response(
        status_code=200,
        headers={
            'Access-Control-Allow-Origin': origin,
            'Access-Control-Allow-Methods': 'GET, OPTIONS',
            'Access-Control-Allow-Headers': '*',
            'Access-Control-Max-Age': '86400',
        }
    )


@router.get('/public/{uid}')
async def get_public_uploads(
    request: Request,
    uid: str,
    limit: int = 50,
    cursor: str | None = None,
    db: Session = Depends(get_db)
):
    """Get public uploads for a user (used by custom domain pages).
    No authentication required - this is for public viewing.
    Only works if user has an enabled custom domain.
    """
    import os
    from datetime import datetime
    from fastapi.responses import JSONResponse
    
    logger.info(f"Public uploads request for uid: {uid}")
    
    # Get the origin for CORS
    origin = request.headers.get('origin', '*')
    
    try:
        # Verify the user has an enabled custom domain
        domain = db.query(UploadsDomain).filter(
            UploadsDomain.uid == uid,
            UploadsDomain.enabled == True
        ).first()
        
        if not domain:
            logger.warning(f"No enabled domain found for uid: {uid}")
            raise HTTPException(status_code=404, detail="No public uploads available")
        
        logger.info(f"Found enabled domain {domain.hostname} for uid: {uid}")
        
        # Get the user's external photos from R2
        photos = []
        prefix = f"users/{uid}/external/"
        
        try:
            # Import R2 client from photos router
            from routers.photos import s3, R2_BUCKET, _get_url_for_key
            
            logger.info(f"R2 configured: s3={s3 is not None}, bucket={R2_BUCKET}, prefix={prefix}")
            
            if s3 and R2_BUCKET:
                client = s3.meta.client
                params = {
                    'Bucket': R2_BUCKET,
                    'Prefix': prefix,
                    'MaxKeys': max(1, min(int(limit or 50), 100)),
                }
                if cursor:
                    params['ContinuationToken'] = cursor
                
                resp = client.list_objects_v2(**params)
                contents = resp.get('Contents', []) or []
                logger.info(f"R2 returned {len(contents)} objects for prefix {prefix}")
                
                for obj in contents:
                    key = obj.get('Key', '')
                    if not key or key.endswith('/') or key.endswith('/_history.txt'):
                        continue
                    name = os.path.basename(key)
                    if '-fromfriend' in name.lower():
                        continue
                    url = _get_url_for_key(key, expires_in=3600)
                    photos.append({
                        'key': key,
                        'url': url,
                        'name': name,
                        'size': obj.get('Size', 0),
                        'last_modified': obj.get('LastModified', datetime.utcnow()).isoformat()
                    })
                
                logger.info(f"Returning {len(photos)} photos for uid {uid}")
                next_token = resp.get('NextContinuationToken') or None
                return JSONResponse(
                    content={'photos': photos, 'next_cursor': next_token},
                    headers={
                        'Access-Control-Allow-Origin': origin,
                        'Access-Control-Allow-Methods': 'GET, OPTIONS',
                        'Access-Control-Allow-Headers': '*',
                    }
                )
            else:
                return JSONResponse(
                    content={'photos': [], 'next_cursor': None},
                    headers={
                        'Access-Control-Allow-Origin': origin,
                        'Access-Control-Allow-Methods': 'GET, OPTIONS',
                        'Access-Control-Allow-Headers': '*',
                    }
                )
        except Exception as ex:
            logger.error(f"R2 error for public uploads {uid}: {ex}")
            raise HTTPException(status_code=500, detail="Storage error")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get public uploads for {uid}: {e}")
        raise HTTPException(status_code=500, detail="Failed to load uploads")
