"""
Vault Domain Router - Custom domain management for vault share pages
Uses dedicated vault_domains table for better security and performance
"""
from fastapi import APIRouter, HTTPException, Request, Depends, Body
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from datetime import datetime
import re
import httpx
import uuid

from core.auth import get_uid_from_request
from core.database import get_db
from core.config import logger
from models.vault_domain import VaultDomain

router = APIRouter(prefix="/api/vaults", tags=["vault-domain"])


def _normalize_domain(dom: str | None) -> str | None:
    if not dom:
        return None
    return dom.strip().lower().rstrip('.')


def _safe_vault_name(vault: str) -> str:
    """Convert vault name to safe identifier"""
    safe = "".join(c for c in vault if c.isalnum() or c in ("-", "_", " ")).strip().replace(" ", "_")
    return safe


@router.get('/domain/config')
async def get_vault_domain_config(
    request: Request,
    vault: str | None = None,
    db: Session = Depends(get_db)
):
    """Get vault domain configuration for the authenticated user"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        if vault:
            # Get domain for specific vault
            safe_vault = _safe_vault_name(vault)
            domain = db.query(VaultDomain).filter(
                VaultDomain.uid == uid,
                VaultDomain.vault_name == safe_vault
            ).first()
            
            if not domain:
                return {"domain": None}
            
            return {"domain": domain.to_dict()}
        else:
            # Get all vault domains for user
            domains = db.query(VaultDomain).filter(VaultDomain.uid == uid).all()
            return {"domains": [d.to_dict() for d in domains]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get vault domain config for {uid}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get domain config: {str(e)}")


@router.post('/domain')
async def set_vault_domain(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db)
):
    """Configure a custom domain for a vault's share page"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")

    hostname = _normalize_domain(payload.get('hostname'))
    vault = payload.get('vault')
    share_token = payload.get('share_token')
    
    if not hostname:
        raise HTTPException(status_code=400, detail="hostname is required")
    if not vault:
        raise HTTPException(status_code=400, detail="vault is required")

    safe_vault = _safe_vault_name(vault)
    
    # Basic hostname validation
    if len(hostname) > 255 or not re.match(r'^[a-z0-9][a-z0-9.-]*[a-z0-9]$', hostname):
        raise HTTPException(status_code=400, detail="invalid hostname")

    try:
        # Check if this vault already has a domain configured
        existing = db.query(VaultDomain).filter(
            VaultDomain.uid == uid,
            VaultDomain.vault_name == safe_vault
        ).first()
        
        if existing:
            if existing.hostname != hostname:
                # Update to new hostname - first check if new hostname is taken
                hostname_taken = db.query(VaultDomain).filter(
                    VaultDomain.hostname == hostname
                ).first()
                if hostname_taken:
                    raise HTTPException(status_code=409, detail="hostname_already_taken")
                
                # Update hostname
                existing.hostname = hostname
                existing.dns_verified = False
                existing.ssl_status = 'unknown'
                existing.enabled = False
                if share_token:
                    existing.share_token = share_token
                existing.updated_at = datetime.utcnow()
                db.commit()
                db.refresh(existing)
                
                return {
                    "success": True,
                    "message": "Domain updated. Create the CNAME record and check status.",
                    "instructions": {
                        "recordType": "CNAME",
                        "name": hostname,
                        "value": "api.photomark.cloud",
                        "ttl": 300
                    },
                    "domain": existing.to_dict()
                }
            
            # Same hostname, update share token if provided
            if share_token:
                existing.share_token = share_token
                db.commit()
                db.refresh(existing)
            
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
        
        # Check if hostname is already taken by another user/vault
        hostname_taken = db.query(VaultDomain).filter(VaultDomain.hostname == hostname).first()
        if hostname_taken:
            raise HTTPException(status_code=409, detail="hostname_already_taken")
        
        # Create new domain record
        domain = VaultDomain(
            id=str(uuid.uuid4()),
            uid=uid,
            vault_name=safe_vault,
            share_token=share_token,
            hostname=hostname,
            dns_verified=False,
            ssl_status='unknown',
            enabled=False
        )
        db.add(domain)
        db.commit()
        db.refresh(domain)
        
        logger.info(f"Created vault domain {hostname} for vault {safe_vault} user {uid[:8]}...")

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
        logger.error(f"Failed to set vault domain for {uid}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to set custom domain: {str(e)}")


@router.post('/domain/remove')
async def remove_vault_domain(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db)
):
    """Remove custom domain from a vault"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    vault = payload.get('vault')
    if not vault:
        raise HTTPException(status_code=400, detail="vault is required")
    
    safe_vault = _safe_vault_name(vault)
    
    try:
        # Only delete domain owned by this user
        domain = db.query(VaultDomain).filter(
            VaultDomain.uid == uid,
            VaultDomain.vault_name == safe_vault
        ).first()
        
        if domain:
            hostname = domain.hostname
            db.delete(domain)
            db.commit()
            logger.info(f"Removed vault domain {hostname} for vault {safe_vault} user {uid[:8]}...")
        
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to remove vault domain for {uid}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to remove domain: {str(e)}")


@router.post('/domain/enable')
async def enable_vault_domain(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db)
):
    """Enable custom domain for a vault (after DNS verified)"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    vault = payload.get('vault')
    if not vault:
        raise HTTPException(status_code=400, detail="vault is required")
    
    safe_vault = _safe_vault_name(vault)
    
    try:
        # Only enable domain owned by this user
        domain = db.query(VaultDomain).filter(
            VaultDomain.uid == uid,
            VaultDomain.vault_name == safe_vault
        ).first()
        
        if not domain:
            raise HTTPException(status_code=404, detail="No domain configured for this vault")
        
        logger.info(f"Enable vault domain {domain.hostname}: dnsVerified={domain.dns_verified}, sslStatus={domain.ssl_status}")
        
        if not domain.dns_verified:
            raise HTTPException(status_code=412, detail=f"domain_not_ready: dns=False")
        
        if domain.ssl_status != 'active':
            raise HTTPException(status_code=412, detail=f"domain_not_ready: ssl={domain.ssl_status}")
        
        domain.enabled = True
        domain.updated_at = datetime.utcnow()
        db.commit()
        
        logger.info(f"Vault domain {domain.hostname} enabled successfully for user {uid[:8]}...")
        return {"ok": True, "domain": domain.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to enable vault domain for {uid}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to enable domain: {str(e)}")


async def _check_domain_dns(hostname: str, db: Session = None, domain_id: str = None) -> dict:
    """Check DNS CNAME and TLS status using Cloudflare DNS over HTTPS."""
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
    if dns_verified and db and domain_id:
        try:
            domain = db.query(VaultDomain).filter(VaultDomain.id == domain_id).first()
            if domain and not domain.dns_verified:
                domain.dns_verified = True
                domain.cname_observed = cname_target
                domain.last_checked = datetime.utcnow()
                db.commit()
                logger.info(f"DNS verified for vault domain {hostname}, updated database for SSL issuance")
        except Exception as e:
            logger.warning(f"Failed to update dnsVerified early: {e}")
            try:
                db.rollback()
            except:
                pass
    
    ssl_status = "unknown"
    if dns_verified:
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=True) as client:
                h = await client.get(f"https://{hostname}", follow_redirects=True)
                if h.status_code < 500:
                    ssl_status = "active"
                    logger.info(f"SSL check for vault domain {hostname}: status={h.status_code}, ssl=active")
                else:
                    ssl_status = "pending"
        except Exception as e:
            ssl_error = str(e)
            err_str = str(e).lower()
            if "certificate" in err_str or "ssl" in err_str or "tls" in err_str:
                ssl_status = "pending"
            else:
                ssl_status = "pending"
            logger.info(f"SSL check for vault domain {hostname}: error - {ssl_error}")
    else:
        ssl_status = "blocked"
    
    return {
        "dnsVerified": dns_verified,
        "sslStatus": ssl_status,
        "cnameObserved": cname_target,
        "error": ssl_error,
    }


@router.get('/domain/status')
async def get_vault_domain_status(
    request: Request,
    vault: str,
    hostname: str | None = None,
    db: Session = Depends(get_db)
):
    """Check DNS CNAME and TLS status for vault custom domain"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")

    safe_vault = _safe_vault_name(vault)

    try:
        # Get domain owned by this user for this vault
        domain = db.query(VaultDomain).filter(
            VaultDomain.uid == uid,
            VaultDomain.vault_name == safe_vault
        ).first()
        
        # Use provided hostname or get from database
        hostname = _normalize_domain(hostname)
        if not hostname and domain:
            hostname = domain.hostname
        
        if not hostname:
            raise HTTPException(status_code=400, detail="No hostname configured for this vault")
        
        # Security check: if domain exists, ensure it belongs to this user
        if domain and domain.hostname != hostname:
            raise HTTPException(status_code=403, detail="Hostname mismatch")

        # Check DNS status
        status = await _check_domain_dns(hostname, db=db, domain_id=domain.id if domain else None)
        
        # Update or create domain record
        if not domain:
            domain = VaultDomain(
                id=str(uuid.uuid4()),
                uid=uid,
                vault_name=safe_vault,
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
        
        logger.info(f"Saved vault domain status for {hostname}: dnsVerified={status['dnsVerified']}, sslStatus={status['sslStatus']}")

        return {
            "hostname": hostname,
            "vaultName": safe_vault,
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
        logger.error(f"Failed to check vault domain status for {uid}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to check domain status: {str(e)}")


@router.post('/domain/update-token')
async def update_vault_domain_token(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db)
):
    """Update the share token for a vault domain"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    vault = payload.get('vault')
    share_token = payload.get('share_token')
    
    if not vault:
        raise HTTPException(status_code=400, detail="vault is required")
    if not share_token:
        raise HTTPException(status_code=400, detail="share_token is required")
    
    safe_vault = _safe_vault_name(vault)
    
    try:
        domain = db.query(VaultDomain).filter(
            VaultDomain.uid == uid,
            VaultDomain.vault_name == safe_vault
        ).first()
        
        if not domain:
            raise HTTPException(status_code=404, detail="No domain configured for this vault")
        
        domain.share_token = share_token
        domain.updated_at = datetime.utcnow()
        db.commit()
        
        return {"ok": True, "domain": domain.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update vault domain token for {uid}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update token: {str(e)}")
