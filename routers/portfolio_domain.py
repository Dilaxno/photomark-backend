"""
Portfolio Domain Router - Custom domain management for portfolio pages
Uses dedicated portfolio_domains table for better security and performance
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
from models.portfolio_domain import PortfolioDomain

router = APIRouter(prefix="/api/portfolio", tags=["portfolio-domain"])


def _normalize_domain(dom: str | None) -> str | None:
    if not dom:
        return None
    return dom.strip().lower().rstrip('.')


@router.get('/domain/config')
async def get_portfolio_domain_config(request: Request, db: Session = Depends(get_db)):
    """Get current portfolio domain configuration for the authenticated user"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        # Query by user UID - ensures user can only see their own domain
        domain = db.query(PortfolioDomain).filter(PortfolioDomain.uid == uid).first()
        
        if not domain:
            return {"domain": {}}
        
        return {"domain": domain.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get portfolio domain config for {uid}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get domain config: {str(e)}")


@router.post('/domain')
async def set_portfolio_domain(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db)
):
    """Configure a custom domain for the user's portfolio page"""
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
        existing = db.query(PortfolioDomain).filter(PortfolioDomain.uid == uid).first()
        
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
                    "value": "photomark.app",
                    "ttl": 300
                }
            }

        # Check if hostname is already taken by another user
        taken = db.query(PortfolioDomain).filter(PortfolioDomain.hostname == hostname).first()
        if taken:
            raise HTTPException(status_code=409, detail="hostname_taken")

        # Generate DNS challenge token
        challenge_token = str(uuid.uuid4())

        # Create new domain record
        domain = PortfolioDomain(
            uid=uid,
            hostname=hostname,
            enabled=False,  # Disabled until DNS is verified
            dns_challenge_token=challenge_token
        )
        db.add(domain)
        db.commit()

        return {
            "success": True,
            "message": "Domain configured successfully. Please set up DNS records.",
            "instructions": {
                "recordType": "CNAME",
                "name": hostname,
                "value": "photomark.app",
                "ttl": 300
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to set portfolio domain for {uid}: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to configure domain: {str(e)}")


@router.post('/domain/remove')
async def remove_portfolio_domain(request: Request, db: Session = Depends(get_db)):
    """Remove custom domain from portfolio"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        domain = db.query(PortfolioDomain).filter(PortfolioDomain.uid == uid).first()
        if not domain:
            raise HTTPException(status_code=404, detail="No domain configured")

        db.delete(domain)
        db.commit()

        return {"success": True, "message": "Domain removed successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to remove portfolio domain for {uid}: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to remove domain: {str(e)}")


@router.post('/domain/enable')
async def enable_portfolio_domain(request: Request, db: Session = Depends(get_db)):
    """Enable custom domain for portfolio (after DNS verified)"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        domain = db.query(PortfolioDomain).filter(PortfolioDomain.uid == uid).first()
        if not domain:
            raise HTTPException(status_code=404, detail="No domain configured")

        if not domain.dns_verified:
            raise HTTPException(status_code=400, detail="DNS not verified")

        domain.enabled = True
        db.commit()

        return {"success": True, "message": "Domain enabled successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to enable portfolio domain for {uid}: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to enable domain: {str(e)}")


async def _check_dns_and_tls_status(hostname: str, db: Session = None, uid: str = None) -> dict:
    """Check DNS CNAME and TLS status using Cloudflare DNS over HTTPS.
    
    If db and uid are provided, updates dnsVerified in database immediately
    so Caddy can issue SSL certificate on the next request.
    """
    try:
        # Check CNAME record via Cloudflare DNS over HTTPS
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://cloudflare-dns.com/dns-query?name={hostname}&type=CNAME",
                headers={"Accept": "application/dns-json"}
            )
            data = resp.json()
        
        cname_valid = False
        cname_target = None
        
        if data.get("Status") == 0 and data.get("Answer"):
            for answer in data["Answer"]:
                if answer.get("type") == 5:  # CNAME record
                    target = answer.get("data", "").rstrip(".")
                    if target == "photomark.app":
                        cname_valid = True
                        cname_target = target
                        break
        
        # If DNS is valid and we have database access, update immediately
        if cname_valid and db and uid:
            try:
                domain = db.query(PortfolioDomain).filter(PortfolioDomain.uid == uid).first()
                if domain and not domain.dns_verified:
                    domain.dns_verified = True
                    domain.dns_verified_at = datetime.utcnow()
                    domain.last_checked = datetime.utcnow()
                    db.commit()
                    logger.info(f"DNS verified for portfolio domain {hostname}, updated database for SSL issuance")
            except Exception as e:
                logger.warning(f"Failed to update dnsVerified early: {e}")
        
        # Check TLS certificate (best effort)
        tls_valid = False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"https://{hostname}", follow_redirects=False)
                tls_valid = resp.status_code < 500
        except Exception:
            pass
        
        return {
            "hostname": hostname,
            "dns": {
                "valid": cname_valid,
                "target": cname_target,
                "expected": "photomark.app"
            },
            "tls": {
                "valid": tls_valid
            }
        }
    
    except Exception as e:
        logger.warning(f"DNS/TLS check failed for {hostname}: {e}")
        return {
            "hostname": hostname,
            "dns": {"valid": False, "error": str(e)},
            "tls": {"valid": False}
        }


@router.get('/domain/status')
async def check_portfolio_domain_status(
    request: Request,
    hostname: str | None = None,
    db: Session = Depends(get_db)
):
    """Check DNS CNAME and TLS status for portfolio custom domain"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        domain = db.query(PortfolioDomain).filter(PortfolioDomain.uid == uid).first()
        
        # Use provided hostname or get from database
        hostname = _normalize_domain(hostname)
        if not hostname and domain:
            hostname = domain.hostname
        
        if not hostname:
            raise HTTPException(status_code=400, detail="No hostname provided or configured")

        # Check DNS and TLS status
        status = await _check_dns_and_tls_status(hostname, db=db, uid=uid)
        
        return {"status": status}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to check portfolio domain status: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to check domain status: {str(e)}")


@router.get('/domain/public/{hostname}')
async def get_public_portfolio_by_domain(hostname: str, db: Session = Depends(get_db)):
    """Get public portfolio for a custom domain (used by Caddy reverse proxy)"""
    try:
        hostname = _normalize_domain(hostname)
        if not hostname:
            raise HTTPException(status_code=400, detail="Invalid hostname")

        # Find domain record
        domain = db.query(PortfolioDomain).filter(
            PortfolioDomain.hostname == hostname,
            PortfolioDomain.enabled == True,
            PortfolioDomain.dns_verified == True
        ).first()

        if not domain:
            raise HTTPException(status_code=404, detail="Domain not found or not verified")

        # Get portfolio data for this user
        from models.portfolio import PortfolioSettings, PortfolioPhoto
        
        settings = db.query(PortfolioSettings).filter(
            PortfolioSettings.uid == domain.uid
        ).first()
        
        if not settings or not settings.is_published:
            raise HTTPException(status_code=404, detail="Portfolio not found or not published")
        
        photos = db.query(PortfolioPhoto).filter(
            PortfolioPhoto.uid == domain.uid
        ).order_by(PortfolioPhoto.order).all()
        
        return {
            "settings": settings.to_dict(),
            "photos": [photo.to_dict() for photo in photos],
            "domain": domain.to_dict()
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get public portfolio for domain {hostname}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get portfolio")