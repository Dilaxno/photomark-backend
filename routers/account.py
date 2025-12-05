from fastapi import APIRouter, Request, Body, Depends, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
import os
import secrets
import shutil
from sqlalchemy.orm import Session

from core.auth import get_uid_from_request, firebase_enabled, fb_auth  # type: ignore
from core.config import logger, STATIC_DIR, s3, R2_BUCKET
from core.database import get_db
from models.user import User
from utils.storage import write_json_key, read_json_key
from utils.emailing import render_email, send_email_smtp

router = APIRouter(prefix="/api/account", tags=["account"]) 

# Create/update PostgreSQL users on signup/login
@router.post("/users/sync")
async def users_sync(request: Request, payload: Optional[dict] = Body(default=None), db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    # Gather name/email from client or Firebase Auth (best-effort)
    name_client = str(((payload or {}).get("name") if payload else "") or "").strip()
    email_client = str(((payload or {}).get("email") if payload else "") or "").strip()
    collab_signup = False

    name_auth = ""
    email_auth = ""
    photo_auth = ""
    user = None
    if firebase_enabled and fb_auth:
        try:
            user = fb_auth.get_user(uid)
            name_auth = (getattr(user, "display_name", None) or "").strip()
            email_auth = (getattr(user, "email", None) or "").strip()
            photo_auth = (getattr(user, "photo_url", None) or "").strip()
        except Exception as ex:
            logger.warning(f"users/sync get_user failed for {uid}: {ex}")

    name = name_client or name_auth
    email = email_client or email_auth

    # Derive a fallback name from email for email/password signups
    if not name and email:
        try:
            name = email.split("@")[0]
        except Exception:
            name = ""

    # Backfill Firebase display_name if missing (common for email/password accounts)
    if firebase_enabled and fb_auth and user and name and not name_auth:
        try:
            fb_auth.update_user(uid, display_name=name)
        except Exception as ex:
            logger.debug(f"users/sync update display_name skipped for {uid}: {ex}")

    try:
        # Check if user exists in PostgreSQL by uid first, then by email
        user = db.query(User).filter(User.uid == uid).first()
        now = datetime.utcnow()
        
        # If no user found by uid, check if email already exists (account linking scenario)
        if not user and email:
            existing_by_email = db.query(User).filter(User.email == email.lower()).first()
            if existing_by_email:
                # Update the existing user's uid to the new one (account linking)
                # This handles cases like: user signed up with email, now signing in with Google
                logger.info(f"users/sync: linking uid {uid} to existing email {email} (old uid: {existing_by_email.uid})")
                existing_by_email.uid = uid
                existing_by_email.display_name = name or existing_by_email.display_name
                if photo_auth or (payload or {}).get("photo_url"):
                    existing_by_email.photo_url = (payload or {}).get("photo_url") or photo_auth or existing_by_email.photo_url
                existing_by_email.last_login_at = now
                existing_by_email.updated_at = now
                db.commit()
                return {"ok": True, "linked": True}
        
        if user:
            # Update existing user
            user.display_name = name or user.display_name
            user.email = email or user.email
            if photo_auth or (payload or {}).get("photo_url"):
                user.photo_url = (payload or {}).get("photo_url") or photo_auth or user.photo_url
            user.last_login_at = now
            user.updated_at = now
        else:
            # Create new user
            user = User(
                uid=uid,
                email=email or f"{uid}@temp.invalid",  # Fallback for missing email
                display_name=name,
                photo_url=((payload or {}).get("photo_url") or photo_auth or None),
                plan="free",
                created_at=now,
                updated_at=now,
                last_login_at=now,
                is_active=True,
                email_verified=False
            )
            db.add(user)
        
        db.commit()
        return {"ok": True}
    except Exception as ex:
        db.rollback()
        logger.exception(f"users/sync failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to sync user profile"}, status_code=500)


def _entitlement_key(uid: str) -> str:
    return f"users/{uid}/billing/entitlement.json"


@router.get("/entitlement")
async def get_entitlement(request: Request, db: Session = Depends(get_db)):
    """Return whether the current user has an active paid entitlement and plan.
    Anonymous users get { isPaid: false, plan: "free" }.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return {"isPaid": False, "plan": "free"}
    try:
        # Get from PostgreSQL
        user = db.query(User).filter(User.uid == uid).first()
        if not user:
            return {"isPaid": False, "plan": "free"}
        
        paid_plans = ["pro", "business", "enterprise", "agencies"]
        is_paid = user.plan in paid_plans
        
        return {"isPaid": is_paid, "plan": user.plan}
    except Exception as ex:
        logger.warning(f"entitlement check failed for {uid}: {ex}")
        return {"isPaid": False, "plan": "free"}

@router.get("/subscription")
async def get_subscription(request: Request, db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        user = db.query(User).filter(User.uid == uid).first()
        if not user:
            return JSONResponse({"error": "NotFound"}, status_code=404)
        return {
            "subscription_id": user.subscription_id or "",
            "status": (user.subscription_status or user.plan or "inactive")
        }
    except Exception as ex:
        logger.warning(f"subscription read failed for {uid}: {ex}")
        return JSONResponse({"error": "ServerError"}, status_code=500)


@router.post("/webhooks/dodo")
async def dodo_webhook(request: Request):
    """
    Webhook receiver for Dodo payment events. On payment.succeeded, updates the
    user's Firestore doc with the new plan and sets isPaid = true immediately.

    Security: If env DODO_WEBHOOK_SECRET is set, require matching X-Dodo-Secret header.
    Expected payload shape (flexible):
      {
        "type": "payment.succeeded",
        "uid": "<firebase-uid>",               # optional if provided in metadata
        "plan": "pro",                          # desired plan
        "data": { "object": { "metadata": { "uid": "...", "plan": "pro" }, "email": "..." } }
      }
    """
    # Verify shared secret if configured
    try:
        secret_expected = os.environ.get("DODO_WEBHOOK_SECRET", "")
        secret_provided = request.headers.get("X-Dodo-Secret") or request.headers.get("x-dodo-secret") or ""
        if secret_expected and secret_provided != secret_expected:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    except Exception:
        # Do not block webhook if header parsing fails unexpectedly
        pass

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    evt_type = str((payload.get("type") or payload.get("event") or "")).lower()

    if evt_type != "payment.succeeded":
        # Acknowledge other events without action
        return {"ok": True}


# ============== Two-Factor Authentication ==============

def _2fa_key(uid: str) -> str:
    return f"auth/2fa/{uid}.json"


def _2fa_totp_pending_key(uid: str) -> str:
    return f"auth/2fa_totp_pending/{uid}.json"


def _2fa_sms_pending_key(uid: str) -> str:
    return f"auth/2fa_sms_pending/{uid}.json"


@router.get("/2fa/status")
async def get_2fa_status(request: Request):
    """
    Get the current 2FA status for the user.
    Returns: { enabled: bool, method: 'totp' | 'sms' | null }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        data = read_json_key(_2fa_key(uid)) or {}
        return {
            "enabled": data.get("enabled", False),
            "method": data.get("method", None)
        }
    except Exception as ex:
        logger.warning(f"2fa/status failed for {uid}: {ex}")
        return {"enabled": False, "method": None}


@router.post("/2fa/totp/init")
async def init_totp_2fa(request: Request):
    """
    Initialize TOTP 2FA setup. Generates a secret and QR code.
    Returns: { secret: str, qr_code: str (data URL) }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        import pyotp
        import qrcode
        import io
        import base64
        
        # Get user email for the TOTP label
        email = ""
        if firebase_enabled and fb_auth:
            try:
                user = fb_auth.get_user(uid)
                email = (getattr(user, "email", None) or "").strip()
            except Exception:
                pass
        
        # Generate a new secret
        secret = pyotp.random_base32()
        
        # Create TOTP URI for QR code
        totp = pyotp.TOTP(secret)
        provisioning_uri = totp.provisioning_uri(
            name=email or uid,
            issuer_name="Photomark"
        )
        
        # Generate QR code as data URL
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(provisioning_uri)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        qr_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
        qr_data_url = f"data:image/png;base64,{qr_base64}"
        
        # Store pending setup
        now = datetime.utcnow()
        pending = {
            "secret": secret,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=30)).isoformat()
        }
        write_json_key(_2fa_totp_pending_key(uid), pending)
        
        return {
            "secret": secret,
            "qr_code": qr_data_url
        }
    except ImportError:
        logger.error("pyotp or qrcode not installed for TOTP 2FA")
        return JSONResponse({"error": "TOTP not available"}, status_code=500)
    except Exception as ex:
        logger.exception(f"2fa/totp/init failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to initialize TOTP"}, status_code=500)


@router.post("/2fa/totp/verify")
async def verify_totp_2fa(request: Request, payload: dict = Body(...)):
    """
    Verify TOTP code and enable 2FA.
    Body: { code: str }
    Returns: { ok: true }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    code = str((payload or {}).get("code") or "").strip()
    if not code or len(code) != 6:
        return JSONResponse({"error": "Invalid code format"}, status_code=400)
    
    try:
        import pyotp
        
        # Get pending setup
        pending = read_json_key(_2fa_totp_pending_key(uid)) or {}
        secret = pending.get("secret")
        exp_str = pending.get("expires_at")
        
        if not secret or not exp_str:
            return JSONResponse({"error": "No pending TOTP setup"}, status_code=400)
        
        # Check expiry
        try:
            exp = datetime.fromisoformat(exp_str)
        except Exception:
            exp = datetime.utcnow() - timedelta(seconds=1)
        
        if datetime.utcnow() > exp:
            write_json_key(_2fa_totp_pending_key(uid), {})
            return JSONResponse({"error": "Setup expired, please start again"}, status_code=400)
        
        # Verify the code
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=1):
            return JSONResponse({"error": "Invalid code"}, status_code=400)
        
        # Enable 2FA
        now = datetime.utcnow()
        data = {
            "enabled": True,
            "method": "totp",
            "secret": secret,
            "enabled_at": now.isoformat()
        }
        write_json_key(_2fa_key(uid), data)
        
        # Clear pending
        write_json_key(_2fa_totp_pending_key(uid), {})
        
        return {"ok": True}
    except ImportError:
        return JSONResponse({"error": "TOTP not available"}, status_code=500)
    except Exception as ex:
        logger.exception(f"2fa/totp/verify failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to verify code"}, status_code=500)


@router.post("/2fa/sms/init")
async def init_sms_2fa(request: Request, payload: dict = Body(...)):
    """
    Initialize SMS 2FA setup. Sends a verification code to the phone.
    Body: { phone: str }
    Returns: { ok: true }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    phone = str((payload or {}).get("phone") or "").strip()
    if not phone:
        return JSONResponse({"error": "Phone number required"}, status_code=400)
    
    # Basic phone validation
    phone_digits = ''.join(c for c in phone if c.isdigit())
    if len(phone_digits) < 10:
        return JSONResponse({"error": "Invalid phone number"}, status_code=400)
    
    try:
        # Generate code
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = datetime.utcnow()
        
        # Store pending setup
        pending = {
            "phone": phone,
            "code": code,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=10)).isoformat(),
            "attempts": 0
        }
        write_json_key(_2fa_sms_pending_key(uid), pending)
        
        # Send SMS via Twilio (if configured)
        twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        twilio_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        twilio_from = os.environ.get("TWILIO_PHONE_NUMBER", "")
        
        if twilio_sid and twilio_token and twilio_from:
            try:
                from twilio.rest import Client
                client = Client(twilio_sid, twilio_token)
                client.messages.create(
                    body=f"Your Photomark verification code is: {code}",
                    from_=twilio_from,
                    to=phone
                )
            except Exception as sms_ex:
                logger.warning(f"SMS send failed for {uid}: {sms_ex}")
                return JSONResponse({"error": "Failed to send SMS"}, status_code=500)
        else:
            # For development/testing, log the code
            logger.info(f"2FA SMS code for {uid}: {code} (Twilio not configured)")
        
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"2fa/sms/init failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to send SMS"}, status_code=500)


@router.post("/2fa/sms/verify")
async def verify_sms_2fa(request: Request, payload: dict = Body(...)):
    """
    Verify SMS code and enable 2FA.
    Body: { code: str, phone: str }
    Returns: { ok: true }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    code = str((payload or {}).get("code") or "").strip()
    phone = str((payload or {}).get("phone") or "").strip()
    
    if not code or len(code) != 6:
        return JSONResponse({"error": "Invalid code format"}, status_code=400)
    
    try:
        # Get pending setup
        pending = read_json_key(_2fa_sms_pending_key(uid)) or {}
        saved_code = pending.get("code")
        saved_phone = pending.get("phone")
        exp_str = pending.get("expires_at")
        attempts = int(pending.get("attempts") or 0)
        
        if not saved_code or not exp_str:
            return JSONResponse({"error": "No pending SMS setup"}, status_code=400)
        
        # Check phone matches
        if phone != saved_phone:
            return JSONResponse({"error": "Phone number mismatch"}, status_code=400)
        
        # Check expiry
        try:
            exp = datetime.fromisoformat(exp_str)
        except Exception:
            exp = datetime.utcnow() - timedelta(seconds=1)
        
        if datetime.utcnow() > exp:
            write_json_key(_2fa_sms_pending_key(uid), {})
            return JSONResponse({"error": "Code expired, please request a new one"}, status_code=400)
        
        # Verify code
        if code != saved_code:
            attempts += 1
            pending["attempts"] = attempts
            if attempts >= 5:
                write_json_key(_2fa_sms_pending_key(uid), {})
                return JSONResponse({"error": "Too many invalid attempts"}, status_code=429)
            write_json_key(_2fa_sms_pending_key(uid), pending)
            return JSONResponse({"error": "Invalid code"}, status_code=400)
        
        # Enable 2FA
        now = datetime.utcnow()
        data = {
            "enabled": True,
            "method": "sms",
            "phone": phone,
            "enabled_at": now.isoformat()
        }
        write_json_key(_2fa_key(uid), data)
        
        # Clear pending
        write_json_key(_2fa_sms_pending_key(uid), {})
        
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"2fa/sms/verify failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to verify code"}, status_code=500)


@router.post("/2fa/disable")
async def disable_2fa(request: Request):
    """
    Disable 2FA for the user.
    Returns: { ok: true }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        # Clear 2FA data and backup codes
        write_json_key(_2fa_key(uid), {"enabled": False, "method": None})
        write_json_key(_2fa_backup_codes_key(uid), {})
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"2fa/disable failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to disable 2FA"}, status_code=500)


def _2fa_backup_codes_key(uid: str) -> str:
    return f"auth/2fa_backup_codes/{uid}.json"


def _generate_backup_codes(count: int = 10) -> list:
    """Generate a list of random backup codes."""
    import string
    codes = []
    for _ in range(count):
        # Generate 8-character alphanumeric codes (uppercase for readability)
        code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        # Format as XXXX-XXXX for readability
        formatted = f"{code[:4]}-{code[4:]}"
        codes.append(formatted)
    return codes


@router.post("/2fa/backup-codes/generate")
async def generate_backup_codes(request: Request):
    """
    Generate new backup codes for 2FA recovery.
    This will invalidate any existing backup codes.
    Returns: { codes: string[], generated_at: string }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        # Check if 2FA is enabled
        twofa_data = read_json_key(_2fa_key(uid)) or {}
        if not twofa_data.get("enabled"):
            return JSONResponse({"error": "2FA must be enabled first"}, status_code=400)
        
        # Generate new backup codes
        codes = _generate_backup_codes(10)
        now = datetime.utcnow()
        
        # Store hashed versions of the codes (for security)
        import hashlib
        hashed_codes = []
        for code in codes:
            # Remove dash for hashing
            clean_code = code.replace("-", "")
            hashed = hashlib.sha256(clean_code.encode()).hexdigest()
            hashed_codes.append({"hash": hashed, "used": False})
        
        backup_data = {
            "codes": hashed_codes,
            "generated_at": now.isoformat(),
            "total": len(codes),
            "remaining": len(codes)
        }
        write_json_key(_2fa_backup_codes_key(uid), backup_data)
        
        # Return the plain codes (only shown once!)
        return {
            "codes": codes,
            "generated_at": now.isoformat()
        }
    except Exception as ex:
        logger.exception(f"2fa/backup-codes/generate failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to generate backup codes"}, status_code=500)


@router.get("/2fa/backup-codes/status")
async def get_backup_codes_status(request: Request):
    """
    Get the status of backup codes (how many remaining, when generated).
    Does NOT return the actual codes.
    Returns: { has_codes: bool, remaining: int, total: int, generated_at: string }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        backup_data = read_json_key(_2fa_backup_codes_key(uid)) or {}
        codes = backup_data.get("codes", [])
        remaining = sum(1 for c in codes if not c.get("used", False))
        
        return {
            "has_codes": len(codes) > 0,
            "remaining": remaining,
            "total": backup_data.get("total", 0),
            "generated_at": backup_data.get("generated_at")
        }
    except Exception as ex:
        logger.exception(f"2fa/backup-codes/status failed for {uid}: {ex}")
        return {"has_codes": False, "remaining": 0, "total": 0, "generated_at": None}


@router.post("/2fa/backup-codes/verify")
async def verify_backup_code(request: Request, payload: dict = Body(...)):
    """
    Verify and consume a backup code for 2FA recovery.
    Body: { code: str }
    Returns: { ok: true, remaining: int }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    code = str((payload or {}).get("code") or "").strip().upper()
    if not code:
        return JSONResponse({"error": "Backup code required"}, status_code=400)
    
    # Remove any dashes for comparison
    clean_code = code.replace("-", "")
    
    try:
        import hashlib
        
        backup_data = read_json_key(_2fa_backup_codes_key(uid)) or {}
        codes = backup_data.get("codes", [])
        
        if not codes:
            return JSONResponse({"error": "No backup codes available"}, status_code=400)
        
        # Hash the provided code and check against stored hashes
        provided_hash = hashlib.sha256(clean_code.encode()).hexdigest()
        
        for i, stored in enumerate(codes):
            if stored.get("hash") == provided_hash and not stored.get("used", False):
                # Mark as used
                codes[i]["used"] = True
                codes[i]["used_at"] = datetime.utcnow().isoformat()
                
                remaining = sum(1 for c in codes if not c.get("used", False))
                backup_data["codes"] = codes
                backup_data["remaining"] = remaining
                write_json_key(_2fa_backup_codes_key(uid), backup_data)
                
                return {"ok": True, "remaining": remaining}
        
        return JSONResponse({"error": "Invalid or already used backup code"}, status_code=400)
    except Exception as ex:
        logger.exception(f"2fa/backup-codes/verify failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to verify backup code"}, status_code=500)
        logger.exception(f"dodo_webhook: failed to update user {uid} plan: {ex}")
        return JSONResponse({"error": "failed to update plan"}, status_code=500)


def _email_change_key(uid: str) -> str:
    return f"auth/email_change/{uid}.json"


def _password_change_key(uid: str) -> str:
    return f"auth/password_change/{uid}.json"


@router.post("/email/change/init")
async def email_change_init(request: Request, payload: dict = Body(...)):
    """
    Start email change with OTP verification. Do NOT change the email yet.
    Body: { "new_email": str }
    Behavior:
      - Generate a 6-digit code
      - Store { new_email, code, expires_at }
      - Send code to the user's CURRENT email via SMTP (Resend-compatible)
    Returns: { ok: true }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    new_email = str((payload or {}).get("new_email") or "").strip()
    if not new_email or "@" not in new_email:
        return JSONResponse({"error": "valid new_email required"}, status_code=400)

    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "email change unavailable"}, status_code=500)

    try:
        # Fetch current email to deliver the code
        user = fb_auth.get_user(uid)
        current_email = (getattr(user, "email", None) or "").strip()
        if not current_email:
            return JSONResponse({"error": "current email unavailable"}, status_code=400)

        # Prepare OTP payload
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = datetime.utcnow()
        rec = {
            "new_email": new_email,
            "code": code,
            "sent_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=15)).isoformat(),
            "attempts": 0,
        }
        write_json_key(_email_change_key(uid), rec)

        # Compose email (Resend SMTP works via SMTP_* env vars)
        subject = "Verify your email change"
        intro = (
            "We received a request to change the email on your account. "
            f"Use this verification code to confirm: <b>{code}</b><br><br>"
            "This code expires in 15 minutes. If you didn't request this, you can ignore this email."
        )
        html = render_email(
            "email_basic.html",
            title="Confirm your email change",
            intro=intro,
            footer_note=f"Request time (UTC): {now.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        ok = send_email_smtp(current_email, subject, html)
        if not ok:
            return JSONResponse({"error": "failed to send verification email"}, status_code=500)
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"email change init failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to start email change"}, status_code=400)


@router.post("/email/change/confirm")
async def email_change_confirm(request: Request, payload: dict = Body(...)):
    """
    Confirm email change with the OTP code and then update Firebase email.
    Body: { "code": str }
    Returns: { ok: true }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    code = str((payload or {}).get("code") or "").strip()
    if not code:
        return JSONResponse({"error": "verification code required"}, status_code=400)

    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "email change unavailable"}, status_code=500)

    try:
        rec = read_json_key(_email_change_key(uid)) or {}
        target_email = str(rec.get("new_email") or "").strip()
        saved_code = str(rec.get("code") or "").strip()
        attempts = int(rec.get("attempts") or 0)
        exp_str = rec.get("expires_at")

        if not target_email or not saved_code or not exp_str:
            return JSONResponse({"error": "no pending email change"}, status_code=400)

        # Expiry check
        try:
            exp = datetime.fromisoformat(exp_str)
        except Exception:
            exp = datetime.utcnow() - timedelta(seconds=1)
        if datetime.utcnow() > exp:
            write_json_key(_email_change_key(uid), {})
            return JSONResponse({"error": "verification code expired"}, status_code=400)

        # Code check
        if code != saved_code:
            attempts += 1
            rec["attempts"] = attempts
            # Optionally lock after too many attempts
            if attempts >= 5:
                write_json_key(_email_change_key(uid), {})
                return JSONResponse({"error": "too many invalid attempts"}, status_code=429)
            write_json_key(_email_change_key(uid), rec)
            return JSONResponse({"error": "invalid verification code"}, status_code=400)

        # Update email now that the code is verified
        try:
            fb_auth.update_user(uid, email=target_email, email_verified=False)
        except Exception as ex:
            logger.warning(f"email change confirm failed for {uid}: {ex}")
            msg = (getattr(ex, "message", None) or str(ex) or "").lower()
            if any(s in msg for s in ("email already exists", "email-already-in-use", "email already in use", "email_exists", "email exists")):
                return JSONResponse({"error": "This email is already used by another account"}, status_code=400)
            return JSONResponse({"error": "Failed to update email"}, status_code=400)

        # Clear pending request
        write_json_key(_email_change_key(uid), {})
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"email change confirm (otp) error for {uid}: {ex}")
        return JSONResponse({"error": "Failed to confirm email change"}, status_code=400)


@router.post("/password/change/init")
async def password_change_init(request: Request):
    """
    Start password change (or set) with OTP verification. Sends a 6-digit code to the
    user's current email. Client should perform reauthentication for email/password
    accounts before calling this (frontend enforced), but server verifies via OTP.
    Returns: { ok: true }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "password change unavailable"}, status_code=500)

    try:
        # Get current email to deliver the code
        user = fb_auth.get_user(uid)
        current_email = (getattr(user, "email", None) or "").strip()
        if not current_email:
            return JSONResponse({"error": "current email unavailable"}, status_code=400)

        code = f"{secrets.randbelow(1_000_000):06d}"
        now = datetime.utcnow()
        rec = {
            "code": code,
            "sent_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=15)).isoformat(),
            "attempts": 0,
        }
        write_json_key(_password_change_key(uid), rec)

        subject = "Verify your password change"
        intro = (
            "We received a request to change the password on your account. "
            f"Use this verification code to confirm: <b>{code}</b><br><br>"
            "This code expires in 15 minutes. If you didn't request this, you can ignore this email."
        )
        html = render_email(
            "email_basic.html",
            title="Confirm your password change",
            intro=intro,
            footer_note=f"Request time (UTC): {now.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        ok = send_email_smtp(current_email, subject, html)
        if not ok:
            return JSONResponse({"error": "failed to send verification email"}, status_code=500)
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"password change init failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to start password change"}, status_code=400)


@router.post("/password/change/confirm")
async def password_change_confirm(request: Request, payload: dict = Body(...)):
    """
    Confirm password change with OTP code and set the new password.
    Body: { "code": str, "new_password": str }
    Returns: { ok: true }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "password change unavailable"}, status_code=500)

    code = str((payload or {}).get("code") or "").strip()
    new_password = str((payload or {}).get("new_password") or "")
    if not code:
        return JSONResponse({"error": "verification code required"}, status_code=400)
    if not new_password or len(new_password) < 6:
        return JSONResponse({"error": "Password must be at least 6 characters"}, status_code=400)

    try:
        rec = read_json_key(_password_change_key(uid)) or {}
        saved_code = str(rec.get("code") or "").strip()
        attempts = int(rec.get("attempts") or 0)
        exp_str = rec.get("expires_at")

        if not saved_code or not exp_str:
            return JSONResponse({"error": "no pending password change"}, status_code=400)

        try:
            exp = datetime.fromisoformat(exp_str)
        except Exception:
            exp = datetime.utcnow() - timedelta(seconds=1)
        if datetime.utcnow() > exp:
            write_json_key(_password_change_key(uid), {})
            return JSONResponse({"error": "verification code expired"}, status_code=400)

        if code != saved_code:
            attempts += 1
            rec["attempts"] = attempts
            if attempts >= 5:
                write_json_key(_password_change_key(uid), {})
                return JSONResponse({"error": "too many invalid attempts"}, status_code=429)
            write_json_key(_password_change_key(uid), rec)
            return JSONResponse({"error": "invalid verification code"}, status_code=400)

        # Update password
        try:
            fb_auth.update_user(uid, password=new_password)
        except Exception as ex:
            logger.warning(f"password change confirm failed for {uid}: {ex}")
            return JSONResponse({"error": "Failed to update password"}, status_code=400)

        # Clear pending request
        write_json_key(_password_change_key(uid), {})
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"password change confirm (otp) error for {uid}: {ex}")
        return JSONResponse({"error": "Failed to confirm password change"}, status_code=400)


@router.post("/plan/cancel")
async def cancel_plan(request: Request, db: Session = Depends(get_db)):
    """
    Cancel the user's current plan and downgrade to free plan.
    Resets isPaid to False and plan to 'free'.
    Returns: { ok: true }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        user = db.query(User).filter(User.uid == uid).first()
        if not user:
            return JSONResponse({"error": "User not found"}, status_code=404)
        
        now = datetime.utcnow()
        user.plan = "free"
        user.updated_at = now
        
        # Update metadata
        metadata = user.extra_metadata or {}
        metadata.update({
            "isPaid": False,
            "cancelledAt": now.isoformat()
        })
        user.extra_metadata = metadata
        
        db.commit()

        # Update local entitlement mirror (best-effort)
        try:
            ent = read_json_key(_entitlement_key(uid)) or {}
            ent.update({"isPaid": False, "plan": "free", "updatedAt": now.isoformat(), "cancelledAt": now.isoformat()})
            write_json_key(_entitlement_key(uid), ent)
        except Exception as ex:
            logger.warning(f"plan/cancel: failed to update entitlement mirror for {uid}: {ex}")

        return {"ok": True}
    except Exception as ex:
        db.rollback()
        logger.exception(f"plan/cancel failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to cancel plan"}, status_code=500)


@router.post("/delete")
async def delete_account(request: Request, db: Session = Depends(get_db)):
    """
    Delete the authenticated user's data and account, then sign them out client-side.
    - Deletes PostgreSQL user record
    - Deletes static files under users/{uid} (local or R2)
    - Deletes Firebase Auth user
    Returns: { ok: true }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "account deletion unavailable"}, status_code=500)

    # 1) PostgreSQL cleanup
    try:
        user = db.query(User).filter(User.uid == uid).first()
        if user:
            db.delete(user)
            db.commit()
    except Exception as ex:
        db.rollback()
        logger.warning(f"delete_account: postgres cleanup failed for {uid}: {ex}")

    # 2) Static files cleanup (local)
    try:
        user_dir = os.path.join(STATIC_DIR, 'users', uid)
        if os.path.isdir(user_dir):
            shutil.rmtree(user_dir, ignore_errors=True)
    except Exception as ex:
        logger.warning(f"delete_account: local static cleanup failed for {uid}: {ex}")

    # 3) R2/S3 cleanup (best-effort, delete all objects with prefix users/{uid}/)
    try:
        if s3 and R2_BUCKET:
            bucket = s3.Bucket(R2_BUCKET)
            bucket.objects.filter(Prefix=f"users/{uid}/").delete()
    except Exception as ex:
        logger.warning(f"delete_account: R2 cleanup failed for {uid}: {ex}")

    # 4) Delete Auth user
    try:
        fb_auth.delete_user(uid)
    except Exception as ex:
        logger.warning(f"delete_account: failed to delete auth user {uid}: {ex}")
        return JSONResponse({"error": "Failed to delete account"}, status_code=400)

    return {"ok": True}


# ============== Brand Kit ==============

class BrandKitPayload(BaseModel):
    logo_url: Optional[str] = None
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None
    accent_color: Optional[str] = None
    background_color: Optional[str] = None
    text_color: Optional[str] = None
    slogan: Optional[str] = None
    font_family: Optional[str] = None
    custom_font_url: Optional[str] = None
    custom_font_name: Optional[str] = None


@router.get("/brand-kit")
async def get_brand_kit(request: Request, db: Session = Depends(get_db)):
    """Get the user's brand kit settings from database."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        user = db.query(User).filter(User.uid == uid).first()
        if not user:
            return {"brand_kit": {}}
        
        data = {
            "logo_url": user.brand_logo_url,
            "primary_color": user.brand_primary_color,
            "secondary_color": user.brand_secondary_color,
            "accent_color": user.brand_accent_color,
            "background_color": user.brand_background_color,
            "text_color": user.brand_text_color,
            "slogan": user.brand_slogan,
            "font_family": user.brand_font_family,
            "custom_font_url": user.brand_custom_font_url,
            "custom_font_name": user.brand_custom_font_name,
        }
        # Remove None values
        data = {k: v for k, v in data.items() if v is not None}
        return {"brand_kit": data}
    except Exception as ex:
        logger.warning(f"get_brand_kit failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to get brand kit"}, status_code=500)


@router.post("/brand-kit")
async def save_brand_kit(request: Request, payload: BrandKitPayload = Body(...), db: Session = Depends(get_db)):
    """Save the user's brand kit settings to database and sync to shop if exists."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        user = db.query(User).filter(User.uid == uid).first()
        if not user:
            return JSONResponse({"error": "User not found"}, status_code=404)
        
        # Update brand kit fields
        if payload.logo_url is not None:
            user.brand_logo_url = payload.logo_url or None
        if payload.primary_color is not None:
            user.brand_primary_color = payload.primary_color or None
        if payload.secondary_color is not None:
            user.brand_secondary_color = payload.secondary_color or None
        if payload.accent_color is not None:
            user.brand_accent_color = payload.accent_color or None
        if payload.background_color is not None:
            user.brand_background_color = payload.background_color or None
        if payload.text_color is not None:
            user.brand_text_color = payload.text_color or None
        if payload.slogan is not None:
            user.brand_slogan = payload.slogan or None
        if payload.font_family is not None:
            user.brand_font_family = payload.font_family or None
        if payload.custom_font_url is not None:
            user.brand_custom_font_url = payload.custom_font_url or None
        if payload.custom_font_name is not None:
            user.brand_custom_font_name = payload.custom_font_name or None
        
        user.updated_at = datetime.utcnow()
        
        # Also sync brand kit to shop theme if user has a shop
        shop_synced = False
        try:
            from models.shop import Shop
            shop = db.query(Shop).filter(Shop.uid == uid).first()
            if shop:
                # Get current theme or create new one
                current_theme = shop.theme if isinstance(shop.theme, dict) else {}
                
                # Update theme with brand kit values (only if brand kit value is set)
                if user.brand_logo_url:
                    current_theme['logoUrl'] = user.brand_logo_url
                if user.brand_primary_color:
                    current_theme['primaryColor'] = user.brand_primary_color
                if user.brand_secondary_color:
                    current_theme['secondaryColor'] = user.brand_secondary_color
                if user.brand_accent_color:
                    current_theme['accentColor'] = user.brand_accent_color
                if user.brand_background_color:
                    current_theme['backgroundColor'] = user.brand_background_color
                if user.brand_text_color:
                    current_theme['textColor'] = user.brand_text_color
                if user.brand_font_family:
                    current_theme['fontFamily'] = user.brand_font_family
                if user.brand_custom_font_url:
                    current_theme['customFontUrl'] = user.brand_custom_font_url
                
                shop.theme = current_theme
                shop.updated_at = datetime.utcnow()
                shop_synced = True
        except Exception as shop_ex:
            logger.warning(f"Failed to sync brand kit to shop for {uid}: {shop_ex}")
        
        db.commit()
        
        # Return updated data
        data = {
            "logo_url": user.brand_logo_url,
            "primary_color": user.brand_primary_color,
            "secondary_color": user.brand_secondary_color,
            "accent_color": user.brand_accent_color,
            "background_color": user.brand_background_color,
            "text_color": user.brand_text_color,
            "slogan": user.brand_slogan,
            "font_family": user.brand_font_family,
            "custom_font_url": user.brand_custom_font_url,
            "custom_font_name": user.brand_custom_font_name,
        }
        data = {k: v for k, v in data.items() if v is not None}
        return {"ok": True, "brand_kit": data, "shop_synced": shop_synced}
    except Exception as ex:
        db.rollback()
        logger.warning(f"save_brand_kit failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to save brand kit"}, status_code=500)


@router.post("/brand-kit/upload-logo")
async def upload_brand_logo(request: Request, file: UploadFile = File(...)):
    """Upload a logo for the brand kit to R2 storage."""
    from utils.storage import upload_bytes, get_presigned_url
    from core.config import R2_CUSTOM_DOMAIN, R2_PUBLIC_BASE_URL
    
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        # Validate file type
        content_type = file.content_type or ""
        if not content_type.startswith("image/"):
            return JSONResponse({"error": "Only image files are allowed"}, status_code=400)
        
        # Read file content
        content = await file.read()
        if len(content) > 5 * 1024 * 1024:  # 5MB limit
            return JSONResponse({"error": "File too large (max 5MB)"}, status_code=400)
        
        # Generate unique filename
        ext = os.path.splitext(file.filename or "logo.png")[1] or ".png"
        key = f"users/{uid}/brand_kit/logo_{secrets.token_urlsafe(8)}{ext}"
        
        # Upload to R2 storage
        upload_bytes(key, content, content_type=content_type)
        
        # For brand kit assets, we need a long-lived URL
        # Try to get a presigned URL with 1 year expiration
        url = get_presigned_url(key, expires_in=86400 * 365)
        
        if not url:
            # Fallback to custom domain URL with https
            base = R2_CUSTOM_DOMAIN or R2_PUBLIC_BASE_URL or ""
            if base:
                # Ensure https:// prefix (strip any existing protocol first)
                base = base.replace("http://", "").replace("https://", "")
                url = f"https://{base.rstrip('/')}/{key}"
            else:
                url = f"/static/{key}"
        
        return {"ok": True, "logo_url": url, "key": key}
    except Exception as ex:
        logger.warning(f"upload_brand_logo failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to upload logo"}, status_code=500)


@router.post("/brand-kit/upload-font")
async def upload_brand_font(request: Request, file: UploadFile = File(...)):
    """Upload a custom font for the brand kit to R2 storage."""
    from utils.storage import upload_bytes, get_presigned_url
    from core.config import R2_CUSTOM_DOMAIN, R2_PUBLIC_BASE_URL
    
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        # Validate file type
        filename = file.filename or "font.ttf"
        ext = os.path.splitext(filename)[1].lower()
        
        allowed_extensions = [".ttf", ".otf", ".woff", ".woff2"]
        if ext not in allowed_extensions:
            return JSONResponse({"error": "Only TTF, OTF, WOFF, WOFF2 fonts are allowed"}, status_code=400)
        
        # Read file content
        content = await file.read()
        if len(content) > 10 * 1024 * 1024:  # 10MB limit
            return JSONResponse({"error": "File too large (max 10MB)"}, status_code=400)
        
        # Generate unique filename
        safe_name = "".join(c for c in os.path.splitext(filename)[0] if c.isalnum() or c in "-_").strip()[:50]
        key = f"users/{uid}/brand_kit/font_{safe_name}_{secrets.token_urlsafe(4)}{ext}"
        
        # Set correct content type for fonts
        font_content_types = {
            ".ttf": "font/ttf",
            ".otf": "font/otf",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
        }
        
        # Upload to R2 storage
        upload_bytes(key, content, content_type=font_content_types.get(ext, "application/octet-stream"))
        
        # For brand kit assets, we need a long-lived URL
        # Try to get a presigned URL with 1 year expiration
        url = get_presigned_url(key, expires_in=86400 * 365)
        
        if not url:
            # Fallback to custom domain URL with https
            base = R2_CUSTOM_DOMAIN or R2_PUBLIC_BASE_URL or ""
            if base:
                # Ensure https:// prefix (strip any existing protocol first)
                base = base.replace("http://", "").replace("https://", "")
                url = f"https://{base.rstrip('/')}/{key}"
            else:
                url = f"/static/{key}"
        
        return {"ok": True, "font_url": url, "font_name": safe_name or "CustomFont", "key": key}
    except Exception as ex:
        logger.warning(f"upload_brand_font failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to upload font"}, status_code=500)
