"""
Integrations router - API tokens for external integrations (Lightroom, etc.)
"""
from fastapi import APIRouter, Request, Body, Depends
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
from typing import Optional
import secrets
import hashlib

from core.auth import get_uid_from_request
from core.config import logger
from utils.storage import read_json_key, write_json_key
from sqlalchemy.orm import Session
from core.database import get_db
from models.user import User

# Plans that have access to Adobe plugins (Lightroom, Photoshop, and future plugins)
# Only Studios and Golden tier plans have plugin access
PLUGIN_ALLOWED_PLANS = ["studios", "golden", "golden_offer"]

# All paid plans (for basic integrations access)
PAID_PLANS = ["individual", "studios", "golden", "golden_offer", "pro", "business", "enterprise", "agencies", "photographers", "team"]


def _get_user_plan(db: Session, uid: str) -> str:
    """Get user's current plan."""
    try:
        user = db.query(User).filter(User.uid == uid).first()
        return user.plan if user else "free"
    except Exception:
        return "free"


def _is_free_user(db: Session, uid: str) -> bool:
    """Check if user is on the free plan."""
    return _get_user_plan(db, uid) not in PAID_PLANS


def _is_plugin_allowed_user(db: Session, uid: str) -> bool:
    """Check if user has a plan that allows plugin access (Studios or Golden)."""
    return _get_user_plan(db, uid) in PLUGIN_ALLOWED_PLANS

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


def _api_tokens_key(uid: str) -> str:
    """Storage key for user's API tokens"""
    return f"users/{uid}/integrations/api_tokens.json"


def _generate_api_token() -> str:
    """Generate a secure API token"""
    return secrets.token_urlsafe(48)


def _hash_token(token: str) -> str:
    """Hash a token for secure storage"""
    return hashlib.sha256(token.encode()).hexdigest()


@router.get("/tokens")
async def list_api_tokens(request: Request, db: Session = Depends(get_db)):
    """
    List all API tokens for the current user.
    Returns token metadata (not the actual tokens).
    NOTE: Integrations are not available for free users.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # Free users cannot access integrations
    if _is_free_user(db, uid):
        return JSONResponse(
            {"error": "Integrations are a premium feature. Please upgrade to a paid plan."},
            status_code=403
        )
    
    try:
        data = read_json_key(_api_tokens_key(uid)) or {}
        tokens = data.get("tokens", [])
        
        # Return metadata only (not the hashed tokens)
        result = []
        for t in tokens:
            result.append({
                "id": t.get("id"),
                "name": t.get("name"),
                "created_at": t.get("created_at"),
                "last_used_at": t.get("last_used_at"),
                "expires_at": t.get("expires_at"),
                "is_active": t.get("is_active", True),
            })
        
        return {"tokens": result}
    except Exception as ex:
        logger.warning(f"list_api_tokens failed for {uid}: {ex}")
        return {"tokens": []}


@router.post("/tokens")
async def create_api_token(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    """
    Create a new API token for integrations (Lightroom, etc.).
    Body: { "name": str (e.g., "Lightroom"), "expires_days": int (optional, default 365) }
    Returns: { "token": str, "id": str, "expires_at": str }
    
    IMPORTANT: The token is only returned once. Store it securely.
    NOTE: This feature is only available for paid users.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # Check if user has a plan that allows plugin access
    if not _is_plugin_allowed_user(db, uid):
        return JSONResponse(
            {"error": "Adobe plugins are available on Studios and Golden plans only. Please upgrade to access this feature."},
            status_code=403
        )
    
    name = str((payload or {}).get("name") or "").strip()
    if not name:
        name = "API Token"
    
    expires_days = int((payload or {}).get("expires_days") or 365)
    if expires_days < 1:
        expires_days = 365
    if expires_days > 3650:  # Max 10 years
        expires_days = 3650
    
    try:
        # Generate token
        token = _generate_api_token()
        token_hash = _hash_token(token)
        token_id = secrets.token_hex(8)
        
        now = datetime.utcnow()
        expires_at = now + timedelta(days=expires_days)
        
        # Load existing tokens
        data = read_json_key(_api_tokens_key(uid)) or {}
        tokens = data.get("tokens", [])
        
        # Limit to 10 tokens per user
        if len(tokens) >= 10:
            return JSONResponse({"error": "Maximum 10 API tokens allowed"}, status_code=400)
        
        # Add new token
        token_record = {
            "id": token_id,
            "name": name,
            "hash": token_hash,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "last_used_at": None,
            "is_active": True,
        }
        tokens.append(token_record)
        
        data["tokens"] = tokens
        data["updated_at"] = now.isoformat()
        write_json_key(_api_tokens_key(uid), data)
        
        # Create lookup entry for fast token verification
        lookup_key = f"auth/api_token_lookup/{token_hash[:16]}.json"
        write_json_key(lookup_key, {
            "uid": uid,
            "token_id": token_id,
            "created_at": now.isoformat(),
        })
        
        # Return the actual token (only shown once!)
        # Format: pm_{uid_prefix}_{token}
        full_token = f"pm_{uid[:8]}_{token}"
        
        return {
            "token": full_token,
            "id": token_id,
            "name": name,
            "expires_at": expires_at.isoformat(),
        }
    except Exception as ex:
        logger.exception(f"create_api_token failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to create token"}, status_code=500)


@router.delete("/tokens/{token_id}")
async def revoke_api_token(request: Request, token_id: str, db: Session = Depends(get_db)):
    """
    Revoke (delete) an API token.
    NOTE: Integrations are not available for free users.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # Free users cannot access integrations
    if _is_free_user(db, uid):
        return JSONResponse(
            {"error": "Integrations are a premium feature. Please upgrade to a paid plan."},
            status_code=403
        )
    
    try:
        data = read_json_key(_api_tokens_key(uid)) or {}
        tokens = data.get("tokens", [])
        
        # Find the token to get its hash for lookup cleanup
        revoked_token = None
        new_tokens = []
        for t in tokens:
            if t.get("id") == token_id:
                revoked_token = t
            else:
                new_tokens.append(t)
        
        if not revoked_token:
            return JSONResponse({"error": "Token not found"}, status_code=404)
        
        # Clean up lookup entry
        if revoked_token.get("hash"):
            lookup_key = f"auth/api_token_lookup/{revoked_token['hash'][:16]}.json"
            write_json_key(lookup_key, {})  # Clear the lookup
        
        data["tokens"] = new_tokens
        data["updated_at"] = datetime.utcnow().isoformat()
        write_json_key(_api_tokens_key(uid), data)
        
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"revoke_api_token failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to revoke token"}, status_code=500)


def verify_api_token(token: str) -> Optional[str]:
    """
    Verify an API token and return the user UID if valid.
    Called by the auth middleware.
    
    Token format: pm_{uid_prefix}_{token}
    """
    if not token or not token.startswith("pm_"):
        return None
    
    try:
        parts = token.split("_", 2)
        if len(parts) != 3:
            return None
        
        _, uid_prefix, actual_token = parts
        token_hash = _hash_token(actual_token)
        
        # We need to find the user by uid prefix
        # This is a bit inefficient but works for now
        # In production, consider using a database index
        
        # For now, we'll need to check all users with matching prefix
        # This is handled by the auth middleware which will pass the full token
        
        return None  # Placeholder - actual verification happens in auth middleware
    except Exception:
        return None


def verify_api_token_for_uid(uid: str, token: str) -> bool:
    """
    Verify an API token for a specific user.
    Returns True if the token is valid for this user.
    """
    if not token or not uid:
        return False
    
    try:
        # Extract the actual token part
        if token.startswith("pm_"):
            parts = token.split("_", 2)
            if len(parts) != 3:
                return False
            actual_token = parts[2]
        else:
            actual_token = token
        
        token_hash = _hash_token(actual_token)
        
        # Load user's tokens
        data = read_json_key(_api_tokens_key(uid)) or {}
        tokens = data.get("tokens", [])
        
        now = datetime.utcnow()
        
        for t in tokens:
            if t.get("hash") == token_hash and t.get("is_active", True):
                # Check expiry
                exp_str = t.get("expires_at")
                if exp_str:
                    try:
                        exp = datetime.fromisoformat(exp_str)
                        if now > exp:
                            continue  # Token expired
                    except Exception:
                        pass
                
                # Update last_used_at
                t["last_used_at"] = now.isoformat()
                write_json_key(_api_tokens_key(uid), data)
                
                return True
        
        return False
    except Exception as ex:
        logger.warning(f"verify_api_token_for_uid failed: {ex}")
        return False
