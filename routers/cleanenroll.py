"""
Cleanenroll OAuth 2.0 Integration Router
Provides OAuth flow, token management, API proxy, and webhook handling.
"""
import os
import secrets
import hmac
import hashlib
from typing import Optional, List
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, Body, Query, HTTPException, Header, BackgroundTasks
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.config import logger
from core.auth import get_uid_from_request
from core.database import get_db
from models.cleanenroll_integration import CleanenrollIntegration

router = APIRouter(prefix="/api/cleanenroll", tags=["cleanenroll"])

# Frontend URL for redirects after OAuth
FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip()

# Cleanenroll OAuth configuration
CLEANENROLL_CLIENT_ID = os.getenv("CLEANENROLL_CLIENT_ID", "")
CLEANENROLL_CLIENT_SECRET = os.getenv("CLEANENROLL_CLIENT_SECRET", "")
CLEANENROLL_REDIRECT_URI = os.getenv("CLEANENROLL_REDIRECT_URI", "")
CLEANENROLL_AUTH_URL = os.getenv("CLEANENROLL_AUTH_URL", "https://app.cleanenroll.com/oauth/authorize")
CLEANENROLL_TOKEN_URL = os.getenv("CLEANENROLL_TOKEN_URL", "https://app.cleanenroll.com/oauth/token")
CLEANENROLL_API_BASE = os.getenv("CLEANENROLL_API_BASE", "https://api.cleanenroll.com/v1")
CLEANENROLL_WEBHOOK_SECRET = os.getenv("CLEANENROLL_WEBHOOK_SECRET", "")

# OAuth state storage (in-memory for simplicity, use Redis in production)
_oauth_states: dict = {}


def _get_integration(db: Session, uid: str) -> Optional[CleanenrollIntegration]:
    """Get user's Cleanenroll integration record."""
    return db.query(CleanenrollIntegration).filter(CleanenrollIntegration.uid == uid).first()


def _create_integration(db: Session, uid: str) -> CleanenrollIntegration:
    """Create a new integration record for user."""
    integration = CleanenrollIntegration(uid=uid)
    db.add(integration)
    db.commit()
    db.refresh(integration)
    return integration


async def _refresh_access_token(db: Session, integration: CleanenrollIntegration) -> Optional[str]:
    """Refresh the access token using refresh token."""
    if not integration.refresh_token:
        return None
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                CLEANENROLL_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": integration.refresh_token,
                    "client_id": CLEANENROLL_CLIENT_ID,
                    "client_secret": CLEANENROLL_CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            
            if resp.status_code != 200:
                logger.warning(f"Cleanenroll token refresh failed: {resp.status_code} - {resp.text}")
                return None
            
            tokens = resp.json()
            
            # Update tokens in database
            integration.access_token = tokens.get("access_token")
            if tokens.get("refresh_token"):
                integration.refresh_token = tokens.get("refresh_token")
            
            expires_in = tokens.get("expires_in", 3600)
            integration.expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
            integration.updated_at = datetime.utcnow()
            
            db.commit()
            
            return integration.access_token
            
    except Exception as ex:
        logger.exception(f"Cleanenroll token refresh error: {ex}")
        return None


async def _ensure_valid_token(db: Session, integration: CleanenrollIntegration) -> Optional[str]:
    """Ensure we have a valid access token, refreshing if needed."""
    if not integration.access_token:
        return None
    
    # Check if token is expired or about to expire (5 min buffer)
    if integration.expires_at:
        if datetime.utcnow() > (integration.expires_at - timedelta(minutes=5)):
            return await _refresh_access_token(db, integration)
    
    return integration.access_token


async def _cleanenroll_api_request(
    access_token: str,
    method: str,
    endpoint: str,
    data: dict = None,
    params: dict = None
) -> dict:
    """Make an authenticated request to Cleanenroll API."""
    url = f"{CLEANENROLL_API_BASE}{endpoint}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        if method.upper() == "GET":
            resp = await client.get(url, headers=headers, params=params)
        elif method.upper() == "POST":
            resp = await client.post(url, headers=headers, json=data)
        elif method.upper() == "PUT":
            resp = await client.put(url, headers=headers, json=data)
        elif method.upper() == "DELETE":
            resp = await client.delete(url, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        
        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="Cleanenroll token expired")
        
        if resp.status_code >= 400:
            logger.warning(f"Cleanenroll API error: {resp.status_code} - {resp.text}")
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        
        return resp.json() if resp.text else {}


# ============ OAuth Endpoints ============

@router.get("/status")
async def cleanenroll_status(request: Request):
    """Check if user has connected their Cleanenroll account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not CLEANENROLL_CLIENT_ID:
        return {"connected": False, "configured": False}
    
    db: Session = next(get_db())
    try:
        integration = _get_integration(db, uid)
        
        if not integration or not integration.is_connected:
            return {"connected": False, "configured": True}
        
        # Check if token is valid
        access_token = await _ensure_valid_token(db, integration)
        if not access_token:
            return {
                "connected": False,
                "configured": True,
                "expired": True,
                "message": "Token expired, please reconnect"
            }
        
        return {
            "connected": True,
            "configured": True,
            "cleanenrollEmail": integration.cleanenroll_email,
            "cleanenrollName": integration.cleanenroll_name,
            "organizationName": integration.organization_name,
            "connectedAt": integration.connected_at.isoformat() if integration.connected_at else None,
            "webhookEnabled": integration.webhook_enabled,
        }
    finally:
        db.close()


@router.get("/auth")
async def cleanenroll_auth(request: Request):
    """Initiate Cleanenroll OAuth flow - returns authorization URL."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not CLEANENROLL_CLIENT_ID or not CLEANENROLL_REDIRECT_URI:
        return JSONResponse({"error": "Cleanenroll integration not configured"}, status_code=500)
    
    # Generate secure state token
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {
        "uid": uid,
        "created_at": datetime.utcnow().isoformat()
    }
    
    # Build authorization URL
    params = {
        "client_id": CLEANENROLL_CLIENT_ID,
        "redirect_uri": CLEANENROLL_REDIRECT_URI,
        "response_type": "code",
        "state": state,
        "scope": "forms:read submissions:read analytics:read payments:read profile:read",
    }
    auth_url = f"{CLEANENROLL_AUTH_URL}?{urlencode(params)}"
    
    return {"auth_url": auth_url}


@router.get("/callback")
async def cleanenroll_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    error_description: str = Query(None)
):
    """Handle Cleanenroll OAuth callback - exchange code for tokens."""
    if error:
        logger.warning(f"Cleanenroll OAuth error: {error} - {error_description}")
        return RedirectResponse(url=f"{FRONTEND_URL}/booking-crm?cleanenroll_error=denied")
    
    if not code or not state:
        return RedirectResponse(url=f"{FRONTEND_URL}/booking-crm?cleanenroll_error=invalid")
    
    # Validate state
    state_data = _oauth_states.pop(state, None)
    if not state_data or not state_data.get("uid"):
        return RedirectResponse(url=f"{FRONTEND_URL}/booking-crm?cleanenroll_error=invalid_state")
    
    uid = state_data["uid"]
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Exchange authorization code for tokens
            token_resp = await client.post(
                CLEANENROLL_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": CLEANENROLL_REDIRECT_URI,
                    "client_id": CLEANENROLL_CLIENT_ID,
                    "client_secret": CLEANENROLL_CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            
            if token_resp.status_code != 200:
                logger.error(f"Cleanenroll token exchange failed: {token_resp.status_code} - {token_resp.text}")
                return RedirectResponse(url=f"{FRONTEND_URL}/booking-crm?cleanenroll_error=token_failed")
            
            tokens = token_resp.json()
            access_token = tokens.get("access_token")
            refresh_token = tokens.get("refresh_token")
            expires_in = tokens.get("expires_in", 3600)
            
            # Fetch user profile from Cleanenroll
            profile_resp = await client.get(
                f"{CLEANENROLL_API_BASE}/me",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            
            profile = {}
            if profile_resp.status_code == 200:
                profile = profile_resp.json()
        
        # Store tokens in database
        db: Session = next(get_db())
        try:
            integration = _get_integration(db, uid)
            if not integration:
                integration = _create_integration(db, uid)
            
            integration.access_token = access_token
            integration.refresh_token = refresh_token
            integration.expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
            integration.is_connected = True
            integration.connected_at = datetime.utcnow()
            integration.cleanenroll_user_id = profile.get("id")
            integration.cleanenroll_email = profile.get("email")
            integration.cleanenroll_name = profile.get("name")
            integration.organization_id = profile.get("organization_id")
            integration.organization_name = profile.get("organization_name")
            integration.webhook_secret = secrets.token_urlsafe(32)
            
            db.commit()
        finally:
            db.close()
        
        return RedirectResponse(url=f"{FRONTEND_URL}/booking-crm?cleanenroll_connected=true")
        
    except Exception as ex:
        logger.exception(f"Cleanenroll callback error: {ex}")
        return RedirectResponse(url=f"{FRONTEND_URL}/booking-crm?cleanenroll_error=unknown")


@router.post("/disconnect")
async def cleanenroll_disconnect(request: Request):
    """Disconnect Cleanenroll account and revoke tokens."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        integration = _get_integration(db, uid)
        if not integration:
            return {"ok": True, "message": "Not connected"}
        
        # Attempt to revoke token at Cleanenroll (best effort)
        if integration.access_token:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(
                        f"{CLEANENROLL_API_BASE}/oauth/revoke",
                        headers={"Authorization": f"Bearer {integration.access_token}"},
                        json={"token": integration.access_token}
                    )
            except Exception as ex:
                logger.warning(f"Token revocation failed (best effort): {ex}")
        
        # Clear integration data
        integration.access_token = None
        integration.refresh_token = None
        integration.expires_at = None
        integration.is_connected = False
        integration.disconnected_at = datetime.utcnow()
        integration.cached_forms = []
        integration.cached_analytics = {}
        
        db.commit()
        
        return {"ok": True, "message": "Disconnected from Cleanenroll"}
    finally:
        db.close()


# ============ Cleanenroll API Proxy Endpoints ============

@router.get("/forms")
async def get_forms(request: Request):
    """Fetch all forms from Cleanenroll."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        integration = _get_integration(db, uid)
        if not integration or not integration.is_connected:
            return JSONResponse({"error": "Cleanenroll not connected"}, status_code=401)
        
        access_token = await _ensure_valid_token(db, integration)
        if not access_token:
            return JSONResponse({"error": "Token expired, please reconnect"}, status_code=401)
        
        data = await _cleanenroll_api_request(access_token, "GET", "/forms")
        
        # Cache forms
        integration.cached_forms = data.get("forms", [])
        integration.cache_updated_at = datetime.utcnow()
        db.commit()
        
        return data
    finally:
        db.close()


@router.get("/forms/{form_id}")
async def get_form(request: Request, form_id: str):
    """Fetch a specific form from Cleanenroll."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        integration = _get_integration(db, uid)
        if not integration or not integration.is_connected:
            return JSONResponse({"error": "Cleanenroll not connected"}, status_code=401)
        
        access_token = await _ensure_valid_token(db, integration)
        if not access_token:
            return JSONResponse({"error": "Token expired, please reconnect"}, status_code=401)
        
        return await _cleanenroll_api_request(access_token, "GET", f"/forms/{form_id}")
    finally:
        db.close()


@router.get("/submissions")
async def get_submissions(
    request: Request,
    form_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0)
):
    """Fetch submissions from Cleanenroll."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        integration = _get_integration(db, uid)
        if not integration or not integration.is_connected:
            return JSONResponse({"error": "Cleanenroll not connected"}, status_code=401)
        
        access_token = await _ensure_valid_token(db, integration)
        if not access_token:
            return JSONResponse({"error": "Token expired, please reconnect"}, status_code=401)
        
        params = {"limit": limit, "offset": offset}
        if form_id:
            params["form_id"] = form_id
        if status:
            params["status"] = status
        
        return await _cleanenroll_api_request(access_token, "GET", "/submissions", params=params)
    finally:
        db.close()


@router.get("/submissions/{submission_id}")
async def get_submission(request: Request, submission_id: str):
    """Fetch a specific submission from Cleanenroll."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        integration = _get_integration(db, uid)
        if not integration or not integration.is_connected:
            return JSONResponse({"error": "Cleanenroll not connected"}, status_code=401)
        
        access_token = await _ensure_valid_token(db, integration)
        if not access_token:
            return JSONResponse({"error": "Token expired, please reconnect"}, status_code=401)
        
        return await _cleanenroll_api_request(access_token, "GET", f"/submissions/{submission_id}")
    finally:
        db.close()


@router.get("/analytics")
async def get_analytics(
    request: Request,
    form_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None)
):
    """Fetch analytics from Cleanenroll."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        integration = _get_integration(db, uid)
        if not integration or not integration.is_connected:
            return JSONResponse({"error": "Cleanenroll not connected"}, status_code=401)
        
        access_token = await _ensure_valid_token(db, integration)
        if not access_token:
            return JSONResponse({"error": "Token expired, please reconnect"}, status_code=401)
        
        params = {}
        if form_id:
            params["form_id"] = form_id
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        
        data = await _cleanenroll_api_request(access_token, "GET", "/analytics", params=params)
        
        # Cache analytics
        integration.cached_analytics = data
        integration.cache_updated_at = datetime.utcnow()
        db.commit()
        
        return data
    finally:
        db.close()


@router.get("/payments")
async def get_payments(
    request: Request,
    form_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0)
):
    """Fetch payments from Cleanenroll."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        integration = _get_integration(db, uid)
        if not integration or not integration.is_connected:
            return JSONResponse({"error": "Cleanenroll not connected"}, status_code=401)
        
        access_token = await _ensure_valid_token(db, integration)
        if not access_token:
            return JSONResponse({"error": "Token expired, please reconnect"}, status_code=401)
        
        params = {"limit": limit, "offset": offset}
        if form_id:
            params["form_id"] = form_id
        if status:
            params["status"] = status
        
        return await _cleanenroll_api_request(access_token, "GET", "/payments", params=params)
    finally:
        db.close()


# ============ Webhook Endpoint ============

@router.post("/webhook")
async def cleanenroll_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_cleanenroll_signature: Optional[str] = Header(None, alias="X-Cleanenroll-Signature")
):
    """
    Receive webhooks from Cleanenroll.
    Verifies signature and processes events.
    """
    body = await request.body()
    
    # Verify webhook signature if configured
    if CLEANENROLL_WEBHOOK_SECRET:
        if not x_cleanenroll_signature:
            logger.warning("Cleanenroll webhook missing signature")
            return JSONResponse({"error": "Missing signature"}, status_code=401)
        
        expected_sig = hmac.new(
            CLEANENROLL_WEBHOOK_SECRET.encode(),
            body,
            hashlib.sha256
        ).hexdigest()
        
        # Compare signatures (timing-safe)
        if not hmac.compare_digest(f"sha256={expected_sig}", x_cleanenroll_signature):
            logger.warning("Cleanenroll webhook signature mismatch")
            return JSONResponse({"error": "Invalid signature"}, status_code=401)
    
    try:
        import json
        payload = json.loads(body)
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    
    event_type = payload.get("event")
    data = payload.get("data", {})
    
    logger.info(f"Cleanenroll webhook received: {event_type}")
    
    # Process webhook in background
    background_tasks.add_task(_process_webhook_event, event_type, data)
    
    return {"ok": True, "received": event_type}


async def _process_webhook_event(event_type: str, data: dict):
    """Process webhook event in background."""
    try:
        # Get user ID from webhook data (organization or user mapping)
        organization_id = data.get("organization_id")
        
        if not organization_id:
            logger.warning(f"Webhook missing organization_id: {event_type}")
            return
        
        db: Session = next(get_db())
        try:
            # Find integration by organization ID
            integration = db.query(CleanenrollIntegration).filter(
                CleanenrollIntegration.organization_id == organization_id,
                CleanenrollIntegration.is_connected == True
            ).first()
            
            if not integration:
                logger.warning(f"No integration found for org {organization_id}")
                return
            
            # Handle different event types
            if event_type == "submission.created":
                # New form submission
                logger.info(f"New submission for user {integration.uid}: {data.get('id')}")
                # Could trigger notifications, update cache, etc.
                
            elif event_type == "submission.updated":
                # Submission status changed
                logger.info(f"Submission updated for user {integration.uid}: {data.get('id')}")
                
            elif event_type == "payment.completed":
                # Payment received
                logger.info(f"Payment completed for user {integration.uid}: {data.get('id')}")
                
            elif event_type == "payment.failed":
                # Payment failed
                logger.info(f"Payment failed for user {integration.uid}: {data.get('id')}")
            
            # Invalidate cache to force refresh
            integration.cache_updated_at = None
            db.commit()
            
        finally:
            db.close()
            
    except Exception as ex:
        logger.exception(f"Webhook processing error: {ex}")


# ============ Dashboard Data Endpoint ============

@router.get("/dashboard")
async def get_dashboard(request: Request):
    """Get aggregated dashboard data for the Cleanenroll integration."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        integration = _get_integration(db, uid)
        if not integration or not integration.is_connected:
            return JSONResponse({"error": "Cleanenroll not connected"}, status_code=401)
        
        access_token = await _ensure_valid_token(db, integration)
        if not access_token:
            return JSONResponse({"error": "Token expired, please reconnect"}, status_code=401)
        
        # Fetch all dashboard data in parallel
        import asyncio
        
        async def fetch_all():
            forms_task = _cleanenroll_api_request(access_token, "GET", "/forms")
            submissions_task = _cleanenroll_api_request(access_token, "GET", "/submissions", params={"limit": 10})
            analytics_task = _cleanenroll_api_request(access_token, "GET", "/analytics")
            payments_task = _cleanenroll_api_request(access_token, "GET", "/payments", params={"limit": 10})
            
            results = await asyncio.gather(
                forms_task, submissions_task, analytics_task, payments_task,
                return_exceptions=True
            )
            return results
        
        forms, submissions, analytics, payments = await fetch_all()
        
        # Handle any errors gracefully
        if isinstance(forms, Exception):
            forms = {"forms": [], "error": str(forms)}
        if isinstance(submissions, Exception):
            submissions = {"submissions": [], "error": str(submissions)}
        if isinstance(analytics, Exception):
            analytics = {"error": str(analytics)}
        if isinstance(payments, Exception):
            payments = {"payments": [], "error": str(payments)}
        
        # Update cache
        integration.cached_forms = forms.get("forms", []) if isinstance(forms, dict) else []
        integration.cached_analytics = analytics if isinstance(analytics, dict) else {}
        integration.cache_updated_at = datetime.utcnow()
        db.commit()
        
        return {
            "forms": forms,
            "recentSubmissions": submissions,
            "analytics": analytics,
            "recentPayments": payments,
            "integration": integration.to_dict()
        }
    finally:
        db.close()
