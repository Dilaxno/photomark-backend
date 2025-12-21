"""
Vault Password Recovery Router
Provides OTP-based vault password recovery for users who forgot their vault passwords
"""
import os
import secrets
import hashlib
from typing import Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Body, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from cryptography.fernet import Fernet
import base64

from core.config import logger
from core.auth import get_uid_from_request, get_user_email_from_uid
from core.database import get_db
from models.vault_password_backup import VaultPasswordBackup, VaultPasswordRecoveryOTP
from utils.emailing import render_email, send_email_smtp

router = APIRouter(prefix="/api", tags=["vault-password-recovery"])

# Encryption key derivation
def _get_encryption_key(uid: str) -> bytes:
    """
    Derive a Fernet encryption key from the user's UID and a secret.
    This ensures passwords are encrypted per-user.
    """
    secret = os.getenv("VAULT_PASSWORD_ENCRYPTION_SECRET", "photomark-vault-secret-key-2024")
    key_material = f"{uid}:{secret}".encode('utf-8')
    # Use SHA256 to get 32 bytes, then base64 encode for Fernet
    key_hash = hashlib.sha256(key_material).digest()
    return base64.urlsafe_b64encode(key_hash)


def _encrypt_password(password: str, uid: str) -> str:
    """Encrypt a vault password"""
    key = _get_encryption_key(uid)
    f = Fernet(key)
    return f.encrypt(password.encode('utf-8')).decode('utf-8')


def _decrypt_password(encrypted: str, uid: str) -> str:
    """Decrypt a vault password"""
    key = _get_encryption_key(uid)
    f = Fernet(key)
    return f.decrypt(encrypted.encode('utf-8')).decode('utf-8')


# ============== BACKUP PASSWORD ENDPOINTS ==============

@router.post("/vault-password/backup")
async def backup_vault_password(
    request: Request,
    vault_name: str = Body(..., embed=True),
    password: str = Body(..., embed=True),
    db: Session = Depends(get_db)
):
    """
    Store an encrypted backup of a vault password.
    Called when user sets/changes a vault password.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        # Sanitize vault name
        safe_vault = "".join(c for c in vault_name if c.isalnum() or c in ("-", "_", " ")).strip().replace(" ", "_")
        if not safe_vault:
            return JSONResponse({"error": "Invalid vault name"}, status_code=400)
        
        # Encrypt the password
        encrypted = _encrypt_password(password, uid)
        
        # Check if backup already exists
        existing = db.query(VaultPasswordBackup).filter(
            VaultPasswordBackup.owner_uid == uid,
            VaultPasswordBackup.vault_name == safe_vault
        ).first()
        
        if existing:
            # Update existing backup
            existing.encrypted_password = encrypted
            existing.updated_at = datetime.utcnow()
        else:
            # Create new backup
            backup = VaultPasswordBackup(
                owner_uid=uid,
                vault_name=safe_vault,
                encrypted_password=encrypted
            )
            db.add(backup)
        
        db.commit()
        return {"ok": True, "message": "Password backup stored"}
        
    except Exception as ex:
        db.rollback()
        logger.error(f"Failed to backup vault password: {ex}")
        return JSONResponse({"error": "Failed to backup password"}, status_code=500)


@router.delete("/vault-password/backup")
async def delete_vault_password_backup(
    request: Request,
    vault_name: str = Body(..., embed=True),
    db: Session = Depends(get_db)
):
    """
    Delete a vault password backup (when vault is deleted or made public).
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        safe_vault = "".join(c for c in vault_name if c.isalnum() or c in ("-", "_", " ")).strip().replace(" ", "_")
        
        db.query(VaultPasswordBackup).filter(
            VaultPasswordBackup.owner_uid == uid,
            VaultPasswordBackup.vault_name == safe_vault
        ).delete()
        
        db.commit()
        return {"ok": True}
        
    except Exception as ex:
        db.rollback()
        logger.error(f"Failed to delete vault password backup: {ex}")
        return JSONResponse({"error": "Failed to delete backup"}, status_code=500)


# ============== OTP RECOVERY ENDPOINTS ==============

@router.post("/vault-password/recovery/request")
async def request_vault_password_recovery(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Request OTP code for vault password recovery.
    Sends a 6-digit code to the user's email.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        # Get user's email
        email = get_user_email_from_uid(uid)
        if not email:
            return JSONResponse({"error": "Email not found for account"}, status_code=400)
        
        # Check rate limiting - max 3 requests per hour
        recent_requests = db.query(VaultPasswordRecoveryOTP).filter(
            VaultPasswordRecoveryOTP.uid == uid,
            VaultPasswordRecoveryOTP.created_at > datetime.utcnow() - timedelta(hours=1)
        ).count()
        
        if recent_requests >= 3:
            return JSONResponse({"error": "Too many requests. Please try again later."}, status_code=429)
        
        # Invalidate any existing unused OTPs
        db.query(VaultPasswordRecoveryOTP).filter(
            VaultPasswordRecoveryOTP.uid == uid,
            VaultPasswordRecoveryOTP.used == False
        ).update({"used": True})
        
        # Generate 6-digit OTP
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires_at = datetime.utcnow() + timedelta(minutes=15)
        
        # Store OTP
        otp = VaultPasswordRecoveryOTP(
            uid=uid,
            email=email,
            code=code,
            expires_at=expires_at
        )
        db.add(otp)
        db.commit()
        
        # Send email
        subject = "Vault Password Recovery Code"
        html = render_email(
            "email_basic.html",
            title="Vault Password Recovery",
            intro=f"Your vault password recovery code is: <strong style='font-size:24px; letter-spacing:4px;'>{code}</strong>",
            button_label="",
            button_url="",
            footer_note="This code will expire in 15 minutes. If you did not request this, please secure your account.",
        )
        text = f"Your vault password recovery code is: {code}\n\nThis code will expire in 15 minutes."
        
        sent = send_email_smtp(email, subject, html, text)
        if not sent:
            return JSONResponse({"error": "Failed to send email"}, status_code=500)
        
        # Mask email for response
        parts = email.split('@')
        if len(parts) == 2:
            local = parts[0]
            domain = parts[1]
            if len(local) > 2:
                masked_local = local[0] + '*' * (len(local) - 2) + local[-1]
            else:
                masked_local = local[0] + '*'
            masked_email = f"{masked_local}@{domain}"
        else:
            masked_email = "***@***"
        
        return {"ok": True, "maskedEmail": masked_email}
        
    except Exception as ex:
        db.rollback()
        logger.error(f"Failed to request vault password recovery: {ex}")
        return JSONResponse({"error": "Failed to send recovery code"}, status_code=500)


@router.post("/vault-password/recovery/verify")
async def verify_vault_password_recovery(
    request: Request,
    code: str = Body(..., embed=True),
    db: Session = Depends(get_db)
):
    """
    Verify OTP code and return all vault passwords if valid.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        # Find the latest unused OTP for this user
        otp = db.query(VaultPasswordRecoveryOTP).filter(
            VaultPasswordRecoveryOTP.uid == uid,
            VaultPasswordRecoveryOTP.used == False
        ).order_by(VaultPasswordRecoveryOTP.created_at.desc()).first()
        
        if not otp:
            return JSONResponse({"error": "No recovery code requested"}, status_code=400)
        
        # Check expiration
        if datetime.utcnow() > otp.expires_at.replace(tzinfo=None):
            return JSONResponse({"error": "Recovery code has expired"}, status_code=410)
        
        # Check attempts
        if otp.attempts >= otp.max_attempts:
            return JSONResponse({"error": "Too many attempts. Please request a new code."}, status_code=429)
        
        # Verify code
        if otp.code != code.strip():
            otp.attempts += 1
            db.commit()
            remaining = otp.max_attempts - otp.attempts
            return JSONResponse({
                "error": f"Invalid code. {remaining} attempts remaining."
            }, status_code=400)
        
        # Mark OTP as verified and used
        otp.verified = True
        otp.used = True
        db.commit()
        
        # Retrieve all vault passwords for this user
        backups = db.query(VaultPasswordBackup).filter(
            VaultPasswordBackup.owner_uid == uid
        ).all()
        
        passwords = []
        for backup in backups:
            try:
                decrypted = _decrypt_password(backup.encrypted_password, uid)
                passwords.append({
                    "vaultName": backup.vault_name,
                    "displayName": backup.vault_name.replace("_", " "),
                    "password": decrypted,
                    "updatedAt": backup.updated_at.isoformat() if backup.updated_at else None
                })
            except Exception as ex:
                logger.warning(f"Failed to decrypt password for vault {backup.vault_name}: {ex}")
                passwords.append({
                    "vaultName": backup.vault_name,
                    "displayName": backup.vault_name.replace("_", " "),
                    "password": None,
                    "error": "Failed to decrypt"
                })
        
        return {
            "ok": True,
            "passwords": passwords,
            "count": len(passwords)
        }
        
    except Exception as ex:
        db.rollback()
        logger.error(f"Failed to verify vault password recovery: {ex}")
        return JSONResponse({"error": "Verification failed"}, status_code=500)


@router.get("/vault-password/has-backups")
async def check_has_password_backups(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Check if user has any vault password backups stored.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        count = db.query(VaultPasswordBackup).filter(
            VaultPasswordBackup.owner_uid == uid
        ).count()
        
        return {"hasBackups": count > 0, "count": count}
        
    except Exception as ex:
        logger.error(f"Failed to check password backups: {ex}")
        return JSONResponse({"error": "Failed to check backups"}, status_code=500)
