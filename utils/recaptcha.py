"""
Google reCAPTCHA v2 verification utility
"""
import os
import httpx
from core.config import logger


async def verify_recaptcha(token: str, remote_ip: str = None) -> bool:
    """
    Verify a reCAPTCHA v2 token with Google's API.
    
    Args:
        token: The reCAPTCHA response token from the client
        remote_ip: Optional IP address of the user
        
    Returns:
        True if verification succeeds, False otherwise
    """
    secret_key = os.getenv("RECAPTCHA_SECRET_KEY", "").strip()
    
    if not secret_key:
        logger.warning("[recaptcha] RECAPTCHA_SECRET_KEY not configured, skipping verification")
        return True  # Fail open in development
    
    if not token:
        logger.warning("[recaptcha] No token provided")
        return False
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://www.google.com/recaptcha/api/siteverify",
                data={
                    "secret": secret_key,
                    "response": token,
                    "remoteip": remote_ip or "",
                },
                timeout=10.0,
            )
            
            if response.status_code != 200:
                logger.error(f"[recaptcha] Verification request failed: {response.status_code}")
                return False
            
            result = response.json()
            success = result.get("success", False)
            
            if not success:
                error_codes = result.get("error-codes", [])
                logger.warning(f"[recaptcha] Verification failed: {error_codes}")
            
            return success
            
    except Exception as ex:
        logger.exception(f"[recaptcha] Verification error: {ex}")
        return False
