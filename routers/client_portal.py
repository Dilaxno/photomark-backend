"""
Client Portal Router
Allows photographer's clients to login, view galleries, and track purchases/downloads
"""
import os
import secrets
import bcrypt
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import APIRouter, Request, Query, HTTPException, Body, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from core.config import logger
from core.auth import get_uid_from_request
from core.database import get_db
from models.client_portal import ClientAccount, ClientGalleryAccess, ClientDownload, ClientPurchase

router = APIRouter(prefix="/api/client-portal", tags=["client-portal"])


# ============ Pydantic Models ============

class ClientCreate(BaseModel):
    email: str
    name: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = []


class ClientUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    is_active: Optional[bool] = None


class GalleryAccessGrant(BaseModel):
    client_id: str
    vault_name: str
    share_token: Optional[str] = None
    display_name: Optional[str] = None
    can_download: Optional[bool] = True
    can_favorite: Optional[bool] = True
    can_comment: Optional[bool] = True
    expires_days: Optional[int] = None


class ClientLoginRequest(BaseModel):
    email: str
    photographer_uid: str


class ClientMagicLinkVerify(BaseModel):
    token: str


class ClientPasswordLogin(BaseModel):
    email: str
    password: str
    photographer_uid: str


class ClientSetPassword(BaseModel):
    token: str  # Magic link token for verification
    password: str


# ============ Helper Functions ============

def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))


def _generate_magic_link_token() -> str:
    return secrets.token_urlsafe(32)


def _generate_client_session_token(client_id: str, photographer_uid: str) -> str:
    """Generate a simple session token for client"""
    return f"client_{client_id}_{photographer_uid}_{secrets.token_urlsafe(16)}"


# ============ Photographer Endpoints (Manage Clients) ============

@router.get("/clients")
async def list_clients(
    request: Request,
    search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    """List all clients for the photographer"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    query = db.query(ClientAccount).filter(ClientAccount.photographer_uid == uid)
    
    if search:
        search_term = f"%{search}%"
        query = query.filter(or_(
            ClientAccount.name.ilike(search_term),
            ClientAccount.email.ilike(search_term),
            ClientAccount.phone.ilike(search_term)
        ))
    
    total = query.count()
    clients = query.order_by(ClientAccount.created_at.desc()).offset(offset).limit(limit).all()
    
    return {
        "clients": [c.to_dict() for c in clients],
        "total": total,
        "limit": limit,
        "offset": offset
    }


@router.post("/clients")
async def create_client(
    request: Request,
    data: ClientCreate,
    db: Session = Depends(get_db)
):
    """Create a new client account"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # Check if client already exists for this photographer
    existing = db.query(ClientAccount).filter(
        ClientAccount.photographer_uid == uid,
        ClientAccount.email == data.email.lower().strip()
    ).first()
    
    if existing:
        return JSONResponse({"error": "Client with this email already exists"}, status_code=400)
    
    client = ClientAccount(
        photographer_uid=uid,
        email=data.email.lower().strip(),
        name=data.name,
        phone=data.phone,
        notes=data.notes,
        tags=data.tags or []
    )
    db.add(client)
    db.commit()
    db.refresh(client)
    
    return client.to_dict()


@router.get("/clients/{client_id}")
async def get_client(
    request: Request,
    client_id: str,
    db: Session = Depends(get_db)
):
    """Get a specific client with their gallery access and activity"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    client = db.query(ClientAccount).filter(
        ClientAccount.id == client_id,
        ClientAccount.photographer_uid == uid
    ).first()
    
    if not client:
        return JSONResponse({"error": "Client not found"}, status_code=404)
    
    # Get gallery access
    galleries = db.query(ClientGalleryAccess).filter(
        ClientGalleryAccess.client_id == client.id
    ).all()
    
    # Get recent downloads
    downloads = db.query(ClientDownload).filter(
        ClientDownload.client_id == client.id
    ).order_by(ClientDownload.downloaded_at.desc()).limit(20).all()
    
    # Get purchases
    purchases = db.query(ClientPurchase).filter(
        ClientPurchase.client_id == client.id
    ).order_by(ClientPurchase.purchased_at.desc()).all()
    
    result = client.to_dict()
    result["galleries"] = [g.to_dict() for g in galleries]
    result["downloads"] = [d.to_dict() for d in downloads]
    result["purchases"] = [p.to_dict() for p in purchases]
    
    return result


@router.put("/clients/{client_id}")
async def update_client(
    request: Request,
    client_id: str,
    data: ClientUpdate,
    db: Session = Depends(get_db)
):
    """Update a client"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    client = db.query(ClientAccount).filter(
        ClientAccount.id == client_id,
        ClientAccount.photographer_uid == uid
    ).first()
    
    if not client:
        return JSONResponse({"error": "Client not found"}, status_code=404)
    
    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        if value is not None:
            setattr(client, key, value)
    
    client.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(client)
    
    return client.to_dict()


@router.delete("/clients/{client_id}")
async def delete_client(
    request: Request,
    client_id: str,
    db: Session = Depends(get_db)
):
    """Delete a client"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    client = db.query(ClientAccount).filter(
        ClientAccount.id == client_id,
        ClientAccount.photographer_uid == uid
    ).first()
    
    if not client:
        return JSONResponse({"error": "Client not found"}, status_code=404)
    
    db.delete(client)
    db.commit()
    
    return {"ok": True, "message": "Client deleted"}


@router.post("/clients/grant-access")
async def grant_gallery_access(
    request: Request,
    data: GalleryAccessGrant,
    db: Session = Depends(get_db)
):
    """Grant a client access to a gallery/vault"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # Verify client belongs to photographer
    client = db.query(ClientAccount).filter(
        ClientAccount.id == data.client_id,
        ClientAccount.photographer_uid == uid
    ).first()
    
    if not client:
        return JSONResponse({"error": "Client not found"}, status_code=404)
    
    # Check if access already exists
    existing = db.query(ClientGalleryAccess).filter(
        ClientGalleryAccess.client_id == client.id,
        ClientGalleryAccess.vault_name == data.vault_name
    ).first()
    
    if existing:
        # Update existing access
        existing.share_token = data.share_token
        existing.display_name = data.display_name
        existing.can_download = data.can_download
        existing.can_favorite = data.can_favorite
        existing.can_comment = data.can_comment
        if data.expires_days:
            existing.expires_at = datetime.utcnow() + timedelta(days=data.expires_days)
        db.commit()
        db.refresh(existing)
        return existing.to_dict()
    
    # Create new access
    access = ClientGalleryAccess(
        client_id=client.id,
        photographer_uid=uid,
        vault_name=data.vault_name,
        share_token=data.share_token,
        display_name=data.display_name,
        can_download=data.can_download,
        can_favorite=data.can_favorite,
        can_comment=data.can_comment,
        expires_at=datetime.utcnow() + timedelta(days=data.expires_days) if data.expires_days else None
    )
    db.add(access)
    db.commit()
    db.refresh(access)
    
    return access.to_dict()


@router.delete("/clients/revoke-access/{access_id}")
async def revoke_gallery_access(
    request: Request,
    access_id: str,
    db: Session = Depends(get_db)
):
    """Revoke a client's access to a gallery"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    access = db.query(ClientGalleryAccess).filter(
        ClientGalleryAccess.id == access_id,
        ClientGalleryAccess.photographer_uid == uid
    ).first()
    
    if not access:
        return JSONResponse({"error": "Access not found"}, status_code=404)
    
    db.delete(access)
    db.commit()
    
    return {"ok": True, "message": "Access revoked"}


@router.post("/clients/{client_id}/send-login-link")
async def send_client_login_link(
    request: Request,
    client_id: str,
    db: Session = Depends(get_db)
):
    """Send a magic login link to a client"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    client = db.query(ClientAccount).filter(
        ClientAccount.id == client_id,
        ClientAccount.photographer_uid == uid
    ).first()
    
    if not client:
        return JSONResponse({"error": "Client not found"}, status_code=404)
    
    # Generate magic link token
    token = _generate_magic_link_token()
    client.magic_link_token = token
    client.magic_link_expires = datetime.utcnow() + timedelta(hours=24)
    db.commit()
    
    # Build login URL
    frontend_origin = os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip()
    login_url = f"{frontend_origin}/client-portal?token={token}"
    
    # Send email
    try:
        from utils.email import send_email_smtp
        from utils.email_templates import render_email
        
        html = render_email(
            "email_basic.html",
            title="Access Your Galleries",
            intro=f"Click the button below to access your photo galleries. This link expires in 24 hours.",
            button_label="View My Galleries",
            button_url=login_url,
        )
        
        send_email_smtp(
            client.email,
            "Access Your Photo Galleries",
            html,
            f"Access your galleries: {login_url}"
        )
    except Exception as e:
        logger.warning(f"Failed to send client login email: {e}")
        return JSONResponse({"error": "Failed to send email"}, status_code=500)
    
    return {"ok": True, "message": "Login link sent"}


# ============ Client-Facing Endpoints (Public) ============

@router.post("/auth/magic-link")
async def request_magic_link(
    data: ClientLoginRequest,
    db: Session = Depends(get_db)
):
    """Request a magic login link (client-facing)"""
    client = db.query(ClientAccount).filter(
        ClientAccount.email == data.email.lower().strip(),
        ClientAccount.photographer_uid == data.photographer_uid,
        ClientAccount.is_active == True
    ).first()
    
    if not client:
        # Don't reveal if client exists
        return {"ok": True, "message": "If an account exists, a login link has been sent"}
    
    # Generate magic link token
    token = _generate_magic_link_token()
    client.magic_link_token = token
    client.magic_link_expires = datetime.utcnow() + timedelta(hours=24)
    db.commit()
    
    # Build login URL
    frontend_origin = os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip()
    login_url = f"{frontend_origin}/client-portal?token={token}"
    
    # Send email
    try:
        from utils.email import send_email_smtp
        from utils.email_templates import render_email
        
        html = render_email(
            "email_basic.html",
            title="Access Your Galleries",
            intro=f"Click the button below to access your photo galleries. This link expires in 24 hours.",
            button_label="View My Galleries",
            button_url=login_url,
        )
        
        send_email_smtp(
            client.email,
            "Access Your Photo Galleries",
            html,
            f"Access your galleries: {login_url}"
        )
    except Exception as e:
        logger.warning(f"Failed to send client login email: {e}")
    
    return {"ok": True, "message": "If an account exists, a login link has been sent"}


@router.post("/auth/verify-magic-link")
async def verify_magic_link(
    data: ClientMagicLinkVerify,
    db: Session = Depends(get_db)
):
    """Verify a magic link and return session token"""
    client = db.query(ClientAccount).filter(
        ClientAccount.magic_link_token == data.token,
        ClientAccount.is_active == True
    ).first()
    
    if not client:
        return JSONResponse({"error": "Invalid or expired link"}, status_code=400)
    
    if client.magic_link_expires and client.magic_link_expires < datetime.utcnow():
        return JSONResponse({"error": "Link has expired"}, status_code=400)
    
    # Clear magic link (one-time use)
    client.magic_link_token = None
    client.magic_link_expires = None
    client.last_login_at = datetime.utcnow()
    client.email_verified = True
    db.commit()
    
    # Generate session token
    session_token = _generate_client_session_token(str(client.id), client.photographer_uid)
    
    return {
        "ok": True,
        "session_token": session_token,
        "client": client.to_dict()
    }


@router.post("/auth/password-login")
async def password_login(
    data: ClientPasswordLogin,
    db: Session = Depends(get_db)
):
    """Login with email and password"""
    client = db.query(ClientAccount).filter(
        ClientAccount.email == data.email.lower().strip(),
        ClientAccount.photographer_uid == data.photographer_uid,
        ClientAccount.is_active == True
    ).first()
    
    if not client or not client.password_hash:
        return JSONResponse({"error": "Invalid email or password"}, status_code=401)
    
    if not _verify_password(data.password, client.password_hash):
        return JSONResponse({"error": "Invalid email or password"}, status_code=401)
    
    client.last_login_at = datetime.utcnow()
    db.commit()
    
    session_token = _generate_client_session_token(str(client.id), client.photographer_uid)
    
    return {
        "ok": True,
        "session_token": session_token,
        "client": client.to_dict()
    }


@router.post("/auth/set-password")
async def set_password(
    data: ClientSetPassword,
    db: Session = Depends(get_db)
):
    """Set password after magic link verification"""
    # Verify token is valid (within last hour of login)
    parts = data.token.split("_")
    if len(parts) < 3 or parts[0] != "client":
        return JSONResponse({"error": "Invalid token"}, status_code=400)
    
    client_id = parts[1]
    client = db.query(ClientAccount).filter(
        ClientAccount.id == client_id,
        ClientAccount.is_active == True
    ).first()
    
    if not client:
        return JSONResponse({"error": "Invalid token"}, status_code=400)
    
    if len(data.password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters"}, status_code=400)
    
    client.password_hash = _hash_password(data.password)
    db.commit()
    
    return {"ok": True, "message": "Password set successfully"}


@router.get("/me")
async def get_client_profile(
    request: Request,
    db: Session = Depends(get_db)
):
    """Get current client's profile (requires session token in header)"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer client_"):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token = auth_header.replace("Bearer ", "")
    parts = token.split("_")
    if len(parts) < 3:
        return JSONResponse({"error": "Invalid token"}, status_code=401)
    
    client_id = parts[1]
    client = db.query(ClientAccount).filter(
        ClientAccount.id == client_id,
        ClientAccount.is_active == True
    ).first()
    
    if not client:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    return client.to_dict()


@router.get("/my-galleries")
async def get_client_galleries(
    request: Request,
    db: Session = Depends(get_db)
):
    """Get all galleries the client has access to"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer client_"):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token = auth_header.replace("Bearer ", "")
    parts = token.split("_")
    if len(parts) < 3:
        return JSONResponse({"error": "Invalid token"}, status_code=401)
    
    client_id = parts[1]
    client = db.query(ClientAccount).filter(
        ClientAccount.id == client_id,
        ClientAccount.is_active == True
    ).first()
    
    if not client:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # Get all gallery access (filter out expired)
    now = datetime.utcnow()
    galleries = db.query(ClientGalleryAccess).filter(
        ClientGalleryAccess.client_id == client.id,
        or_(
            ClientGalleryAccess.expires_at == None,
            ClientGalleryAccess.expires_at > now
        )
    ).order_by(ClientGalleryAccess.granted_at.desc()).all()
    
    return {"galleries": [g.to_dict() for g in galleries]}


@router.get("/my-downloads")
async def get_client_downloads(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db)
):
    """Get client's download history"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer client_"):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token = auth_header.replace("Bearer ", "")
    parts = token.split("_")
    if len(parts) < 3:
        return JSONResponse({"error": "Invalid token"}, status_code=401)
    
    client_id = parts[1]
    client = db.query(ClientAccount).filter(
        ClientAccount.id == client_id,
        ClientAccount.is_active == True
    ).first()
    
    if not client:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    downloads = db.query(ClientDownload).filter(
        ClientDownload.client_id == client.id
    ).order_by(ClientDownload.downloaded_at.desc()).limit(limit).all()
    
    return {"downloads": [d.to_dict() for d in downloads]}


@router.get("/my-purchases")
async def get_client_purchases(
    request: Request,
    db: Session = Depends(get_db)
):
    """Get client's purchase history"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer client_"):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token = auth_header.replace("Bearer ", "")
    parts = token.split("_")
    if len(parts) < 3:
        return JSONResponse({"error": "Invalid token"}, status_code=401)
    
    client_id = parts[1]
    client = db.query(ClientAccount).filter(
        ClientAccount.id == client_id,
        ClientAccount.is_active == True
    ).first()
    
    if not client:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    purchases = db.query(ClientPurchase).filter(
        ClientPurchase.client_id == client.id
    ).order_by(ClientPurchase.purchased_at.desc()).all()
    
    return {"purchases": [p.to_dict() for p in purchases]}
