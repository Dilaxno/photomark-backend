from fastapi import APIRouter, Request, Body, HTTPException, Depends
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
import os
import secrets
import random
import hashlib
from datetime import datetime, timedelta

from core.auth import get_uid_from_request, firebase_enabled, fb_auth  # type: ignore
from core.config import logger
from utils.emailing import render_email, send_email_smtp
from utils.storage import write_json_key, read_json_key
from utils.rate_limit import signup_throttle
from sqlalchemy.orm import Session
from core.database import SessionLocal, get_db
from models.user import User
from models.user_security import UserSecurity, PasswordResetRequest, SMSVerificationCode
from utils.validation import validate_signup_data, validate_email, validate_email_mx
from utils.recaptcha import verify_recaptcha

router = APIRouter(prefix="/api", tags=["auth"])


# ---- Email validation ----

@router.post("/auth/validate-email")
async def validate_email_endpoint(payload: dict = Body(...)):
    """
    Validate email address format and MX records in real-time.
    Body: { "email": str }
    Returns: { "valid": bool, "error": str | null }
    """
    email = (payload.get("email") or "").strip()
    
    if not email:
        return {"valid": False, "error": "Email is required"}
    
    # First check basic email format
    is_valid_format, format_error = validate_email(email)
    if not is_valid_format:
        return {"valid": False, "error": format_error}
    
    # Then check MX records
    is_valid_mx, mx_error = validate_email_mx(email)
    if not is_valid_mx:
        return {"valid": False, "error": mx_error}
    
    return {"valid": True, "error": None}


# ---- Signup rate limiting ----

@router.post("/auth/signup/check")
async def auth_signup_check(request: Request, payload: dict = Body(None)):
    """
    Check if signup is allowed from this IP address.
    Validates name and email for gibberish.
    Rate limit: 1 signup per IP per 6 hours.
    Body (optional): { "name": str, "email": str }
    """
    # Get client IP
    ip = request.client.host if request.client else "unknown"
    
    # Check for X-Forwarded-For header (when behind proxy/load balancer)
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # Take the first IP in the chain (client IP)
        ip = forwarded.split(",")[0].strip()
    
    if ip == "unknown":
        logger.warning("[auth.signup] Could not determine client IP")
        return JSONResponse({"error": "Could not verify request"}, status_code=400)
    
    # Verify reCAPTCHA if token provided
    if payload and payload.get("recaptchaToken"):
        recaptcha_token = payload.get("recaptchaToken", "").strip()
        is_valid_captcha = await verify_recaptcha(recaptcha_token, ip)
        if not is_valid_captcha:
            logger.warning(f"[auth.signup] reCAPTCHA verification failed for IP {ip}")
            return JSONResponse(
                {
                    "error": "reCAPTCHA verification failed. Please try again.",
                    "allowed": False
                },
                status_code=400
            )
    
    # Server-side validation of name and email if provided
    if payload:
        name = (payload.get("name") or "").strip()
        email = (payload.get("email") or "").strip()
        
        if name and email:
            is_valid, error_msg = validate_signup_data(name, email)
            if not is_valid:
                logger.warning(f"[auth.signup] Validation failed for IP {ip}: {error_msg}")
                return JSONResponse(
                    {
                        "error": error_msg,
                        "allowed": False
                    },
                    status_code=400
                )
    
    try:
        # Check current rate limit state without consuming quota
        state = signup_throttle.peek(ip)
        
        # If remaining is 0, rate limit is exceeded
        if state.remaining <= 0:
            return JSONResponse(
                {
                    "error": "Too many signup attempts from this IP address. Please try again later.",
                    "allowed": False,
                    "retry_after": "6 hours"
                },
                status_code=429
            )
        
        return {
            "allowed": True,
            "remaining": state.remaining,
            "ip": ip  # Return for debugging (remove in production if needed)
        }
    except Exception as ex:
        logger.exception(f"[auth.signup] Rate limit check failed: {ex}")
        # Fail open - allow signup if rate limiter fails
        return {"allowed": True}

@router.post("/auth/signin/check")
async def auth_signin_check(request: Request, payload: dict = Body(...)):
    ip = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    recaptcha_token = (payload or {}).get("recaptchaToken", "").strip()
    if not recaptcha_token:
        return JSONResponse({"error": "Missing reCAPTCHA token"}, status_code=400)
    is_valid_captcha = await verify_recaptcha(recaptcha_token, ip)
    if not is_valid_captcha:
        logger.warning(f"[auth.signin] reCAPTCHA verification failed for IP {ip}")
        return JSONResponse({"error": "reCAPTCHA verification failed. Please try again."}, status_code=400)
    return {"allowed": True}


@router.post("/auth/signup/consume")
async def auth_signup_consume(request: Request):
    """
    Consume signup quota after successful signup.
    Call this AFTER Firebase creates the user.
    """
    # Get client IP
    ip = request.client.host if request.client else "unknown"
    
    # Check for X-Forwarded-For header
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    
    if ip == "unknown":
        logger.warning("[auth.signup] Could not determine client IP for consume")
        return {"ok": True}  # Don't block signup if we can't get IP
    
    try:
        # Consume 1 quota for this IP (record the signup)
        result = signup_throttle.limit(ip, cost=1)
        logger.info(f"[auth.signup] Consumed signup quota for IP: {ip}, limited={result.limited}")
        return {"ok": True, "ip": ip, "limited": result.limited}
    except Exception as ex:
        logger.exception(f"[auth.signup] Quota consume failed: {ex}")
        return {"ok": True}  # Don't fail the signup if rate limiter fails


# ---- Helpers (key structures) ----

def _pw_reset_key(token: str) -> str:
    return f"auth/password_resets/{token}.json"


def _user_meta_key(uid: str) -> str:
    return f"users/{uid}/meta.json"


def _email_verification_key(token: str) -> str:
    return f"auth/email_verifications/{token}.json"


# ---- Password reset flow (OTP-based) ----

def _pw_reset_otp_key(email: str) -> str:
    """Key for storing OTP password reset data by email"""
    import hashlib
    email_hash = hashlib.sha256(email.lower().encode()).hexdigest()[:16]
    return f"auth/password_reset_otp/{email_hash}.json"

@router.post("/auth/password/reset/request")
async def auth_password_reset_request(request: Request, payload: dict = Body(...)):
    """
    Send OTP code to email for password reset.
    Body: { "email": str }
    Returns: { ok: true }
    
    SECURITY: Always returns success to prevent email enumeration attacks.
    """
    from utils.rate_limit import password_reset_throttle
    
    email = (payload or {}).get("email", "").strip().lower()
    if not email:
        return JSONResponse({"error": "email required"}, status_code=400)
    
    # Rate limit by IP AND email to prevent enumeration/abuse
    ip = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    
    try:
        # Rate limit by IP (stricter)
        ip_result = password_reset_throttle.limit(f"pw_reset_ip:{ip}", cost=1)
        if ip_result.limited:
            logger.warning(f"[auth.password_reset] Rate limited IP: {ip}")
            return JSONResponse({"error": "Too many requests. Please try again later."}, status_code=429)
        
        # Rate limit by email
        result = password_reset_throttle.limit(f"pw_reset:{email}", cost=1)
        if result.limited:
            logger.warning(f"[auth.password_reset] Rate limited for email: {email}")
            # SECURITY: Return success to prevent enumeration
            return {"ok": True}
    except Exception as ex:
        logger.warning(f"[auth.password_reset] Rate limit check failed: {ex}")
    
    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "password reset unavailable"}, status_code=500)

    try:
        # Verify user exists
        user = fb_auth.get_user_by_email(email)
        uid = getattr(user, "uid", None)
        if not uid:
            # SECURITY: Return success even if user doesn't exist (prevent enumeration)
            logger.info(f"[auth.password_reset] Email not found (hidden): {email}")
            return {"ok": True}

        # Generate 6-digit OTP
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = datetime.utcnow()
        exp = now + timedelta(minutes=15)  # 15 minute expiry
        
        rec = {
            "code": code,
            "uid": uid,
            "email": email,
            "created_at": now.isoformat(),
            "expires_at": exp.isoformat(),
            "verified": False,
        }
        write_json_key(_pw_reset_otp_key(email), rec)

        # Send email with OTP
        subject = "Reset your password"
        html = render_email(
            "email_basic.html",
            title="Reset your password",
            intro=f"Your password reset code is: <strong style='font-size:24px; letter-spacing:4px;'>{code}</strong>",
            button_label="",
            button_url="",
            footer_note="This code will expire in 15 minutes. If you did not request this, you can ignore this email.",
        )
        text = f"Your password reset code is: {code}\n\nThis code will expire in 15 minutes."

        sent = send_email_smtp(email, subject, html, text)
        if not sent:
            return JSONResponse({"error": "Failed to send email"}, status_code=500)
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"Password reset OTP request failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/auth/password/reset/verify")
async def auth_password_reset_verify(payload: dict = Body(...)):
    """
    Verify OTP code for password reset.
    Body: { "email": str, "otp": str }
    Returns: { ok: true }
    """
    email = (payload or {}).get("email", "").strip().lower()
    otp = (payload or {}).get("otp", "").strip()
    
    if not email or not otp:
        return JSONResponse({"error": "Please enter the verification code"}, status_code=400)
    
    rec = read_json_key(_pw_reset_otp_key(email))
    if not rec:
        return JSONResponse({"error": "Verification code is incorrect or has expired"}, status_code=400)
    
    try:
        exp = datetime.fromisoformat(str(rec.get("expires_at", "")))
    except Exception:
        return JSONResponse({"error": "Verification code is incorrect or has expired"}, status_code=400)
    
    now = datetime.utcnow()
    if now > exp:
        return JSONResponse({"error": "Verification code has expired. Please request a new one."}, status_code=410)
    
    if rec.get("code") != otp:
        return JSONResponse({"error": "Verification code is incorrect"}, status_code=400)
    
    # Mark as verified
    rec["verified"] = True
    write_json_key(_pw_reset_otp_key(email), rec)
    
    return {"ok": True}


@router.post("/auth/password/reset/confirm")
async def auth_password_reset_confirm_otp(payload: dict = Body(...)):
    """
    Reset password using verified OTP.
    Body: { "email": str, "otp": str, "password": str }
    Returns: { ok: true }
    """
    email = (payload or {}).get("email", "").strip().lower()
    otp = (payload or {}).get("otp", "").strip()
    password = (payload or {}).get("password", "").strip()
    
    if not email or not otp or not password:
        return JSONResponse({"error": "Please fill in all required fields"}, status_code=400)
    
    if len(password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters"}, status_code=400)
    
    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "Password reset is temporarily unavailable"}, status_code=500)
    
    rec = read_json_key(_pw_reset_otp_key(email))
    if not rec:
        return JSONResponse({"error": "Verification code is incorrect or has expired"}, status_code=400)
    
    if not rec.get("verified"):
        return JSONResponse({"error": "Please verify the code first"}, status_code=400)
    
    try:
        exp = datetime.fromisoformat(str(rec.get("expires_at", "")))
    except Exception:
        return JSONResponse({"error": "Verification code is incorrect or has expired"}, status_code=400)
    
    now = datetime.utcnow()
    if now > exp:
        return JSONResponse({"error": "Verification code has expired. Please request a new one."}, status_code=410)
    
    if rec.get("code") != otp:
        return JSONResponse({"error": "Verification code is incorrect"}, status_code=400)
    
    uid = rec.get("uid")
    if not uid:
        return JSONResponse({"error": "Invalid reset data"}, status_code=400)
    
    try:
        # Update password in Firebase
        fb_auth.update_user(uid, password=password)
        
        # Delete the OTP record
        write_json_key(_pw_reset_otp_key(email), {})
        
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"Password reset confirm failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


# ---- Alternative Password Recovery Methods (Database-backed) ----

def _hash_backup_code(code: str) -> str:
    """Hash a backup code for secure storage"""
    return hashlib.sha256(code.upper().encode()).hexdigest()


def _mask_phone(phone: str) -> str:
    """Mask phone number for display"""
    if not phone or len(phone) < 7:
        return "****"
    return phone[:3] + "****" + phone[-4:]


@router.post("/auth/password/reset/secondary-email")
async def auth_password_reset_secondary_email(payload: dict = Body(...), db: Session = Depends(get_db)):
    """
    Request password reset via secondary email.
    Body: { "secondary_email": str }
    Returns: { ok: true }
    """
    secondary_email = (payload or {}).get("secondary_email", "").strip().lower()
    
    if not secondary_email:
        return JSONResponse({"error": "secondary_email required"}, status_code=400)
    
    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "password reset unavailable"}, status_code=500)
    
    try:
        # Find user by secondary email in database
        # First check UserSecurity table
        security = db.query(UserSecurity).filter(
            UserSecurity.secondary_email == secondary_email,
            UserSecurity.secondary_email_verified == True
        ).first()
        
        # Also check User table's secondary_email field
        if not security:
            user_record = db.query(User).filter(User.secondary_email == secondary_email).first()
            if user_record:
                found_uid = user_record.uid
                found_primary_email = user_record.email
            else:
                return JSONResponse({"error": "Secondary email not found or not verified"}, status_code=404)
        else:
            found_uid = security.uid
            # Get primary email
            user_record = db.query(User).filter(User.uid == found_uid).first()
            if not user_record:
                return JSONResponse({"error": "User not found"}, status_code=404)
            found_primary_email = user_record.email
        
        # Generate OTP
        code = "".join([str(random.randint(0, 9)) for _ in range(6)])
        expires_at = datetime.utcnow() + timedelta(minutes=15)
        
        # Store in database
        reset_request = PasswordResetRequest(
            uid=found_uid,
            email=found_primary_email,
            code=code,
            verified=False,
            method="secondary_email",
            expires_at=expires_at
        )
        db.add(reset_request)
        db.commit()
        
        # Also store in JSON for backward compatibility with existing confirm endpoint
        write_json_key(_pw_reset_otp_key(found_primary_email), {
            "code": code,
            "uid": found_uid,
            "expires_at": expires_at.isoformat(),
            "verified": False,
            "method": "secondary_email"
        })
        
        # Send email to secondary email
        try:
            html = f"""
            <h2>Password Reset Code</h2>
            <p>Your password reset code is:</p>
            <h1 style="font-size: 32px; letter-spacing: 8px; font-family: monospace;">{code}</h1>
            <p>This code expires in 15 minutes.</p>
            <p>If you didn't request this, please ignore this email.</p>
            """
            text = f"Your password reset code is: {code}\n\nThis code expires in 15 minutes."
            send_email_smtp(secondary_email, "Password Reset Code - Photomark", html, text)
        except Exception as ex:
            logger.warning(f"Failed to send secondary email: {ex}")
        
        # Return primary email so frontend can use it for verification
        return {"ok": True, "email": found_primary_email}
        
    except Exception as ex:
        logger.exception(f"Secondary email reset failed: {ex}")
        return JSONResponse({"error": "Failed to process request"}, status_code=500)


@router.post("/auth/password/reset/backup-code")
async def auth_password_reset_backup_code(payload: dict = Body(...), db: Session = Depends(get_db)):
    """
    Verify identity using 2FA backup code.
    Body: { "email": str, "backup_code": str }
    Returns: { ok: true, reset_token: str }
    """
    email = (payload or {}).get("email", "").strip().lower()
    backup_code = (payload or {}).get("backup_code", "").strip().upper()
    
    if not email or not backup_code:
        return JSONResponse({"error": "email and backup_code required"}, status_code=400)
    
    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "password reset unavailable"}, status_code=500)
    
    try:
        user = fb_auth.get_user_by_email(email)
        uid = user.uid
    except Exception:
        return JSONResponse({"error": "User not found"}, status_code=404)
    
    # Check backup codes in database
    security = db.query(UserSecurity).filter(UserSecurity.uid == uid).first()
    
    if not security or not security.backup_codes:
        return JSONResponse({"error": "No backup codes configured for this account"}, status_code=400)
    
    # Hash the provided code and check against stored hashes
    code_hash = _hash_backup_code(backup_code)
    codes = security.backup_codes or []
    
    # Check if code matches (support both hashed and plain codes for migration)
    code_found = False
    if code_hash in codes:
        codes.remove(code_hash)
        code_found = True
    elif backup_code in codes:
        codes.remove(backup_code)
        code_found = True
    
    if not code_found:
        return JSONResponse({"error": "Invalid backup code"}, status_code=400)
    
    # Update backup codes in database
    security.backup_codes = codes
    security.updated_at = datetime.utcnow()
    db.commit()
    
    # Generate reset token
    reset_token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(minutes=15)
    
    # Store reset request in database
    reset_request = PasswordResetRequest(
        uid=uid,
        email=email,
        code=reset_token,
        verified=True,  # Already verified via backup code
        method="backup_code",
        expires_at=expires_at
    )
    db.add(reset_request)
    db.commit()
    
    # Also store in JSON for backward compatibility
    write_json_key(_pw_reset_otp_key(email), {
        "code": reset_token,
        "uid": uid,
        "expires_at": expires_at.isoformat(),
        "verified": True,
        "method": "backup_code"
    })
    
    return {"ok": True, "reset_token": reset_token}


@router.post("/auth/password/reset/sms")
async def auth_password_reset_sms_request(payload: dict = Body(...), db: Session = Depends(get_db)):
    """
    Request SMS verification for password reset.
    Body: { "email": str }
    Returns: { ok: true, masked_phone: str }
    """
    email = (payload or {}).get("email", "").strip().lower()
    
    if not email:
        return JSONResponse({"error": "email required"}, status_code=400)
    
    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "password reset unavailable"}, status_code=500)
    
    try:
        user = fb_auth.get_user_by_email(email)
        uid = user.uid
    except Exception:
        return JSONResponse({"error": "User not found"}, status_code=404)
    
    # Get 2FA phone number from database
    security = db.query(UserSecurity).filter(UserSecurity.uid == uid).first()
    
    if not security or not security.phone_number or not security.phone_verified:
        return JSONResponse({"error": "No verified phone number configured for 2FA"}, status_code=400)
    
    phone = security.phone_number
    
    # Generate SMS code
    code = "".join([str(random.randint(0, 9)) for _ in range(6)])
    expires_at = datetime.utcnow() + timedelta(minutes=10)
    
    # Store SMS code in database
    # First, invalidate any existing codes for this user
    db.query(SMSVerificationCode).filter(
        SMSVerificationCode.uid == uid,
        SMSVerificationCode.purpose == "password_reset",
        SMSVerificationCode.used == False
    ).update({"used": True})
    
    sms_code = SMSVerificationCode(
        uid=uid,
        code=code,
        purpose="password_reset",
        phone_number=phone,
        expires_at=expires_at
    )
    db.add(sms_code)
    db.commit()
    
    # Send SMS (integrate with your SMS provider - Twilio, etc.)
    try:
        # TODO: Integrate with SMS provider
        # For now, log the code (remove in production!)
        logger.info(f"SMS code for {email}: {code}")
        
        # Example Twilio integration:
        # from twilio.rest import Client
        # TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
        # TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
        # TWILIO_PHONE = os.getenv("TWILIO_PHONE_NUMBER")
        # if TWILIO_SID and TWILIO_TOKEN and TWILIO_PHONE:
        #     client = Client(TWILIO_SID, TWILIO_TOKEN)
        #     client.messages.create(
        #         body=f"Your Photomark reset code: {code}",
        #         from_=TWILIO_PHONE,
        #         to=phone
        #     )
    except Exception as ex:
        logger.warning(f"Failed to send SMS: {ex}")
    
    return {"ok": True, "masked_phone": _mask_phone(phone)}


@router.post("/auth/password/reset/sms/verify")
async def auth_password_reset_sms_verify(payload: dict = Body(...), db: Session = Depends(get_db)):
    """
    Verify SMS code for password reset.
    Body: { "email": str, "code": str }
    Returns: { ok: true, reset_token: str }
    """
    email = (payload or {}).get("email", "").strip().lower()
    code = (payload or {}).get("code", "").strip()
    
    if not email or not code:
        return JSONResponse({"error": "email and code required"}, status_code=400)
    
    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "password reset unavailable"}, status_code=500)
    
    try:
        user = fb_auth.get_user_by_email(email)
        uid = user.uid
    except Exception:
        return JSONResponse({"error": "User not found"}, status_code=404)
    
    # Verify SMS code from database
    sms_record = db.query(SMSVerificationCode).filter(
        SMSVerificationCode.uid == uid,
        SMSVerificationCode.purpose == "password_reset",
        SMSVerificationCode.used == False
    ).order_by(SMSVerificationCode.created_at.desc()).first()
    
    if not sms_record:
        return JSONResponse({"error": "No SMS code requested"}, status_code=400)
    
    if datetime.utcnow() > sms_record.expires_at.replace(tzinfo=None):
        return JSONResponse({"error": "SMS code has expired"}, status_code=410)
    
    # Check attempts
    if sms_record.attempts >= sms_record.max_attempts:
        return JSONResponse({"error": "Too many attempts. Please request a new code."}, status_code=429)
    
    if sms_record.code != code:
        sms_record.attempts += 1
        db.commit()
        return JSONResponse({"error": "Invalid SMS code"}, status_code=400)
    
    # Mark SMS code as used
    sms_record.used = True
    db.commit()
    
    # Generate reset token
    reset_token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(minutes=15)
    
    # Store reset request in database
    reset_request = PasswordResetRequest(
        uid=uid,
        email=email,
        code=reset_token,
        verified=True,  # Already verified via SMS
        method="sms",
        expires_at=expires_at
    )
    db.add(reset_request)
    db.commit()
    
    # Also store in JSON for backward compatibility
    write_json_key(_pw_reset_otp_key(email), {
        "code": reset_token,
        "uid": uid,
        "expires_at": expires_at.isoformat(),
        "verified": True,
        "method": "sms"
    })
    
    return {"ok": True, "reset_token": reset_token}


# ---- Password reset flow (Token-based, legacy) ----

@router.post("/auth/password/reset")
async def auth_password_reset(request: Request, payload: dict = Body(...)):
    email = (payload or {}).get("email", "").strip()
    if not email:
        return JSONResponse({"error": "email required"}, status_code=400)
    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "password reset unavailable"}, status_code=500)

    try:
        # Resolve user uid from email
        user = fb_auth.get_user_by_email(email)
        uid = getattr(user, "uid", None)
        if not uid:
            return JSONResponse({"error": "account not found"}, status_code=404)

        # Generate token and persist (1 hour)
        token = secrets.token_urlsafe(32)
        now = datetime.utcnow()
        exp = now + timedelta(hours=1)
        rec = {
            "token": token,
            "uid": uid,
            "email": email,
            "created_at": now.isoformat(),
            "expires_at": exp.isoformat(),
            "used": False,
        }
        write_json_key(_pw_reset_key(token), rec)

        # Build link to frontend handler
        front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "").rstrip("/") or "https://photomark.cloud"
        link = f"{front}/#newpassword?token={token}"

        subject = "Reset your password"
        html = render_email(
            "email_basic.html",
            title="Reset your password",
            intro="Click the button below to reset your password.",
            button_label="Reset password",
            button_url=link,
            footer_note="If you did not request this, you can ignore this email.",
        )
        text = f"Open this link to reset your password: {link}"

        sent = send_email_smtp(email, subject, html, text)
        if not sent:
            return JSONResponse({"error": "Failed to send email"}, status_code=500)
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"Password reset init failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/auth/password/validate")
async def auth_password_validate(token: str):
    if not token or len(token) < 10:
        return JSONResponse({"error": "invalid token"}, status_code=400)
    rec = read_json_key(_pw_reset_key(token))
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        exp = datetime.fromisoformat(str(rec.get("expires_at", "")))
    except Exception:
        exp = None
    now = datetime.utcnow()
    if exp and now > exp:
        return JSONResponse({"error": "expired"}, status_code=410)
    if rec.get("used"):
        return JSONResponse({"error": "consumed"}, status_code=410)
    return {"email": rec.get("email") or ""}


@router.post("/auth/password/confirm")
async def auth_password_confirm(payload: dict = Body(...)):
    token = str((payload or {}).get("token") or "").strip()
    password = str((payload or {}).get("password") or "").strip()
    if not token or not password:
        return JSONResponse({"error": "token and password required"}, status_code=400)
    if len(password) < 6:
        return JSONResponse({"error": "password too short"}, status_code=400)
    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "password reset unavailable"}, status_code=500)

    rec = read_json_key(_pw_reset_key(token))
    if not rec:
        return JSONResponse({"error": "invalid token"}, status_code=400)
    try:
        exp = datetime.fromisoformat(str(rec.get("expires_at", "")))
    except Exception:
        exp = None
    now = datetime.utcnow()
    if exp and now > exp:
        return JSONResponse({"error": "expired"}, status_code=410)
    if rec.get("used"):
        return JSONResponse({"error": "consumed"}, status_code=410)

    uid = rec.get("uid") or ""
    try:
        fb_auth.update_user(uid, password=password)
        rec["used"] = True
        write_json_key(_pw_reset_key(token), rec)
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"Password update failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


# ---- Welcome email ----

@router.post("/email/welcome")
async def email_welcome(request: Request):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "Email not available"}, status_code=500)
    try:
        user = fb_auth.get_user(uid)
        email = getattr(user, "email", None)
        if not email:
            return {"ok": False}
        meta = read_json_key(_user_meta_key(uid)) or {}
        if meta.get("welcome_sent"):
            return {"ok": True}
        app_name = os.getenv("APP_NAME", "Photomark")
        subject = f"Welcome to {app_name}"
        link = (os.getenv('FRONTEND_ORIGIN', '').split(',')[0].rstrip('/') or '') + '#software'
        html = render_email(
            "email_basic.html",
            title=f"Welcome to {app_name}",
            intro=f"Hi {getattr(user, 'display_name', '') or ''},<br>Welcome! You're all set to watermark, convert, and style your photos.",
            button_label="Get started",
            button_url=link,
            footer_note="Happy creating!",
        )
        text = f"Welcome to {app_name}! Get started by uploading your first photos."
        send_email_smtp(email, subject, html, text)
        meta["welcome_sent"] = True
        write_json_key(_user_meta_key(uid), meta)
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"Welcome email failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


# ---- Email verification ----

@router.post("/auth/email/verification/send")
async def auth_email_verification_send(request: Request):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "Email verification unavailable"}, status_code=500)
    try:
        user = fb_auth.get_user(uid)
        email = getattr(user, "email", None)
        if not email:
            return JSONResponse({"error": "No email on account"}, status_code=400)
        if getattr(user, "email_verified", False):
            return {"ok": True, "already_verified": True}

        # Throttle using user meta
        meta = read_json_key(_user_meta_key(uid)) or {}
        now = datetime.utcnow()
        last_sent_iso = meta.get("verify_sent_at")
        if last_sent_iso:
            try:
                last = datetime.fromisoformat(str(last_sent_iso))
                if (now - last).total_seconds() < 60:
                    return {"ok": True, "throttled": True}
            except Exception:
                pass

        token = secrets.token_urlsafe(32)
        exp = now + timedelta(hours=48)
        rec = {
            "token": token,
            "uid": uid,
            "email": email,
            "created_at": now.isoformat(),
            "expires_at": exp.isoformat(),
            "used": False,
        }
        write_json_key(_email_verification_key(token), rec)

        base = os.getenv("PUBLIC_URL", "https://api.photomark.cloud").rstrip("/")
        link = f"{base}/api/auth/email/verification/confirm?token={token}"

        subject = "Verify your email"
        html = render_email(
            "email_basic.html",
            title="Verify your email",
            intro="Please verify your email address for your account.",
            button_label="Verify email",
            button_url=link,
            footer_note="If you did not create this account, you can ignore this email.",
        )
        text = f"Verify your email by opening this link: {link}"

        sent = send_email_smtp(email, subject, html, text)
        if not sent:
            return JSONResponse({"error": "Failed to send email"}, status_code=500)

        meta["verify_sent_at"] = now.isoformat()
        write_json_key(_user_meta_key(uid), meta)
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"Email verification send failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/auth/email/verification/confirm")
async def auth_email_verification_confirm(token: str, request: Request):
    if not token or len(token) < 10:
        return JSONResponse({"error": "invalid token"}, status_code=400)
    rec = read_json_key(_email_verification_key(token))
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        exp = datetime.fromisoformat(str(rec.get("expires_at", "")))
    except Exception:
        exp = None
    now = datetime.utcnow()
    if exp and now > exp:
        return JSONResponse({"error": "expired"}, status_code=410)

    uid = rec.get("uid") or ""
    try:
        # Mark verified in Firebase
        if firebase_enabled and fb_auth:
            fb_auth.update_user(uid, email_verified=True)
        rec["used"] = True
        write_json_key(_email_verification_key(token), rec)
        # Mirror verification status to Neon (PostgreSQL)
        try:
            db: Session = SessionLocal()
            try:
                u = db.query(User).filter(User.uid == uid).first()
                if u:
                    u.email_verified = True
                    u.updated_at = datetime.utcnow()
                    db.commit()
            finally:
                db.close()
        except Exception:
            # Best-effort: don't block verification on DB failure
            logger.warning(f"[auth.verify] Failed to mirror email_verified for uid={uid}")
    except Exception as ex:
        logger.exception(f"Set email verified failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)

    # Mint a Firebase custom token to allow auto-login on frontend
    custom_jwt = None
    try:
        if firebase_enabled and fb_auth:
            ct_bytes = fb_auth.create_custom_token(uid)
            custom_jwt = ct_bytes.decode("utf-8") if isinstance(ct_bytes, (bytes, bytearray)) else str(ct_bytes)
    except Exception as ex:
        logger.warning(f"Custom token creation failed: {ex}")

    fe = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
    if custom_jwt:
        # Use path-based route instead of hash so clients that strip '#' still work
        return RedirectResponse(url=f"{fe}/verify-success?ct={custom_jwt}", status_code=302)

    # Fallback success page
    body = "<h1>Email verified</h1><p>Your email has been verified successfully.</p>"
    if fe:
        body += f"<p><a href=\"{fe}\">Continue to app</a></p>"
    html_page = f"<!doctype html><html><head><meta charset='utf-8'><title>Verified</title></head><body>{body}</body></html>"
    return HTMLResponse(content=html_page, status_code=200)
