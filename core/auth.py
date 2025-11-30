import os
from typing import Optional, Tuple
from fastapi import Request
from core.config import logger
from utils.storage import read_json_key

def resolve_workspace_uid(request: Request) -> tuple[Optional[str], Optional[str]]:
    req_uid = get_uid_from_request(request)
    if not req_uid:
        return None, None
    return req_uid, req_uid


firebase_enabled = False
try:
    import firebase_admin
    from firebase_admin import auth as fb_auth, credentials as fb_credentials
    # Firestore is no longer used for persistence.

    FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
    FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    FIREBASE_SERVICE_ACCOUNT_JSON_PATH = (os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON_PATH", "") or "").strip().strip('"').strip("'")

    if not getattr(firebase_admin, "_apps", []):
        if FIREBASE_SERVICE_ACCOUNT_JSON:
            import json
            cred = fb_credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT_JSON))
            firebase_admin.initialize_app(cred, {"projectId": FIREBASE_PROJECT_ID} if FIREBASE_PROJECT_ID else None)
        elif FIREBASE_SERVICE_ACCOUNT_JSON_PATH and os.path.isfile(FIREBASE_SERVICE_ACCOUNT_JSON_PATH):
            from os import environ
            if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
                environ["GOOGLE_APPLICATION_CREDENTIALS"] = FIREBASE_SERVICE_ACCOUNT_JSON_PATH
            cred = fb_credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_JSON_PATH)
            firebase_admin.initialize_app(cred, {"projectId": FIREBASE_PROJECT_ID} if FIREBASE_PROJECT_ID else None)
        else:
            firebase_admin.initialize_app(options={"projectId": FIREBASE_PROJECT_ID} if FIREBASE_PROJECT_ID else None)
    firebase_enabled = True
    logger.info("Firebase Admin initialized")
except Exception as ex:
    logger.warning(f"Firebase Admin not initialized: {ex}")
    fb_auth = None  # type: ignore
    fb_fs = None  # type: ignore






def get_uid_from_request(request: Request) -> Optional[str]:
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return None
    
    if not firebase_enabled or not fb_auth:
        return None
    try:
        decoded = fb_auth.verify_id_token(token)
        return decoded.get("uid")
    except Exception as ex:
        logger.warning(f"Token verification failed: {ex}")
        return None


def get_user_email_from_uid(uid: str) -> Optional[str]:
    try:
        if not firebase_enabled or not fb_auth:
            return None
        user = fb_auth.get_user(uid)
        return (getattr(user, "email", None) or "").lower()
    except Exception as ex:
        logger.warning(f"get_user_email_from_uid failed: {ex}")
        return None


def get_uid_by_email(email: str) -> Optional[str]:
    try:
        if not email:
            return None
        if not firebase_enabled or not fb_auth:
            return None
        user = fb_auth.get_user_by_email(email)
        return getattr(user, "uid", None)
    except Exception as ex:
        logger.warning(f"get_uid_by_email failed: {ex}")
        return None


def require_admin(request: Request, admin_emails: list[str]) -> Tuple[bool, str]:
    try:
        if not firebase_enabled or not fb_auth:
            return False, "auth disabled"
        uid = get_uid_from_request(request)
        if not uid:
            return False, "unauthorized"
        user = fb_auth.get_user(uid)
        email = (getattr(user, "email", None) or "").lower()
        if email and (email in admin_emails):
            return True, email
        return False, email or ""
    except Exception as ex:
        logger.warning(f"require_admin failed: {ex}")
        return False, "error"
