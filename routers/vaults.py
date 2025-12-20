from typing import List, Optional, Tuple
import os
import json
import secrets
import io
import zipfile
import httpx
import asyncio
import qrcode
import subprocess
import tempfile
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Body, UploadFile, File, Form, BackgroundTasks, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
import bcrypt

from core.config import s3, s3_presign_client, R2_BUCKET, R2_PUBLIC_BASE_URL, R2_CUSTOM_DOMAIN, logger, DODO_API_BASE, DODO_CHECKOUT_PATH, DODO_PRODUCTS_PATH, DODO_API_KEY, DODO_WEBHOOK_SECRET, LICENSE_SECRET, LICENSE_PRIVATE_KEY, LICENSE_PUBLIC_KEY, LICENSE_ISSUER
from utils.storage import read_json_key, write_json_key, read_bytes_key, upload_bytes, get_presigned_url
from utils.metadata import auto_embed_metadata_for_user
from core.auth import get_uid_from_request, get_user_email_from_uid
from utils.emailing import render_email, send_email_smtp
from utils.sendbird import create_vault_channel, ensure_sendbird_user, sendbird_api
from sqlalchemy.orm import Session
from sqlalchemy import text, func
from core.database import get_db
from models.gallery import GalleryAsset
from models.user import User
from models.vault_trash import VaultTrash, VaultVersion

router = APIRouter(prefix="/api", tags=["vaults"])


async def _track_download_analytics(
    request: Request,
    owner_uid: str,
    vault_name: str,
    share_token: str,
    download_type: str,
    photo_keys: List[str],
    file_count: int,
    total_size_bytes: Optional[int] = None,
    is_paid: bool = False,
    payment_amount_cents: Optional[int] = None,
    payment_id: Optional[str] = None
):
    """Track download analytics asynchronously"""
    try:
        from models.analytics import DownloadEvent
        from core.database import get_db
        
        # Generate analytics data
        visitor_hash = hashlib.sha256(f"{request.client.host}:{request.headers.get('user-agent', '')}".encode()).hexdigest()[:32]
        ip_hash = hashlib.sha256(f"ip:{request.client.host}".encode()).hexdigest()[:32]
        
        # Parse user agent
        ua_string = request.headers.get("user-agent", "")
        ua_lower = ua_string.lower()
        if "mobile" in ua_lower or "android" in ua_lower or "iphone" in ua_lower:
            device_type = "mobile"
        elif "tablet" in ua_lower or "ipad" in ua_lower:
            device_type = "tablet"
        else:
            device_type = "desktop"
        
        # Extract browser info (simple)
        browser = "unknown"
        if "chrome" in ua_lower:
            browser = "Chrome"
        elif "firefox" in ua_lower:
            browser = "Firefox"
        elif "safari" in ua_lower and "chrome" not in ua_lower:
            browser = "Safari"
        elif "edge" in ua_lower:
            browser = "Edge"
        
        # Get referrer source
        referrer = request.headers.get("referer", "")
        source = "direct"
        if referrer:
            referrer_lower = referrer.lower()
            if any(s in referrer_lower for s in ["facebook", "instagram", "twitter", "linkedin"]):
                source = "social"
            elif any(s in referrer_lower for s in ["google", "bing", "yahoo"]):
                source = "search"
            elif "mail" in referrer_lower:
                source = "email"
            else:
                source = "referral"
        
        # Create download event
        db = next(get_db())
        try:
            download_event = DownloadEvent(
                owner_uid=owner_uid,
                vault_name=vault_name,
                share_token=share_token,
                download_type=download_type,
                photo_keys=photo_keys,
                file_count=file_count,
                total_size_bytes=total_size_bytes,
                visitor_hash=visitor_hash,
                ip_hash=ip_hash,
                device_type=device_type,
                browser=browser,
                os="unknown",
                is_paid=is_paid,
                payment_amount_cents=payment_amount_cents,
                payment_id=payment_id,
                referrer=referrer,
                source=source
            )
            db.add(download_event)
            db.commit()
            logger.info(f"Download analytics tracked: {download_type} for vault {vault_name}")
        except Exception as e:
            logger.error(f"Failed to track download analytics: {e}")
            db.rollback()
        finally:
            db.close()
            
    except Exception as e:
        logger.error(f"Error in download analytics tracking: {e}")


def _get_url_for_key(key: str, expires_in: int = 3600) -> str:
    return get_presigned_url(key, expires_in=expires_in) or ""



class CheckoutPayload(BaseModel):
    token: str



class ApprovalPayload(BaseModel):
    token: str
    key: str
    action: str  # 'approve' or 'deny'
    comment: str | None = None

class FavoritePayload(BaseModel):
    token: str
    key: str
    favorite: bool

class RetouchRequestPayload(BaseModel):
    token: str
    key: str
    comment: Optional[str] | None = None
    annotations: Optional[dict] | None = None
    markups: Optional[list] | None = None
    marked_photo_url: Optional[str] | None = None

# Local static dir used when s3 is not configured
STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))

# Special vault name for grouping photos sent by friends/partners (safe identifier)
FRIENDS_VAULT_SAFE = "Photos_sent_by_friends"


def _share_key(token: str) -> str:
    return f"shares/{token}.json"


def _approval_key(uid: str, vault: str) -> str:
    safe = "".join(c for c in vault if c.isalnum() or c in ("-", "_", " ")).strip().replace(" ", "_")
    return f"users/{uid}/vaults/_approvals/{safe}.json"

def _favorites_key(uid: str, vault: str) -> str:
    safe = "".join(c for c in vault if c.isalnum() or c in ("-", "_", " ")).strip().replace(" ", "_")
    return f"users/{uid}/vaults/_favorites/{safe}.json"

# Lightweight versioning helpers for real-time polling/streaming

def _approvals_version_key(uid: str, vault: str) -> str:
    safe = _vault_key(uid, vault)[1]
    return f"users/{uid}/vaults/_approvals/{safe}.ver.json"


def _retouch_version_key(uid: str, vault: str) -> str:
    safe = _vault_key(uid, vault)[1]
    return f"users/{uid}/retouch/_ver/{safe}.json"


def _touch_version(key: str):
    try:
        _write_json_key(key, {"updated_at": datetime.utcnow().isoformat()})
    except Exception:
        pass


def _read_version(key: str) -> str:
    try:
        rec = _read_json_key(key) or {}
        return str(rec.get("updated_at") or "")
    except Exception:
        return ""


def _touch_approvals_version(uid: str, vault: str):
    _touch_version(_approvals_version_key(uid, vault))


def _touch_retouch_version(uid: str, vault: str):
    _touch_version(_retouch_version_key(uid, vault))

# Retouch queue helpers (per-user global queue)

def _retouch_queue_key(uid: str) -> str:
    return f"users/{uid}/retouch/queue.json"


def _read_retouch_queue(uid: str) -> list[dict]:
    data = _read_json_key(_retouch_queue_key(uid)) or []
    try:
        if isinstance(data, list):
            return data
        # Migrate old map to list if needed
        if isinstance(data, dict) and data.get("items"):
            items = data.get("items")
            return items if isinstance(items, list) else []
    except Exception:
        pass
    return []


def _write_retouch_queue(uid: str, items: list[dict]):
    # Persist as a flat list for simplicity
    _write_json_key(_retouch_queue_key(uid), items or [])


from utils.invisible_mark import detect_signature, PAYLOAD_LEN
from io import BytesIO
from PIL import Image


def _cache_key_for_invisible(uid: str, photo_key: str) -> str:
    h = hashlib.sha1(photo_key.encode('utf-8')).hexdigest()
    return f"users/{uid}/_cache/invisible/{h}.json"


def _has_invisible_mark(uid: str, key: str) -> bool:
    try:
        ckey = _cache_key_for_invisible(uid, key)
        rec = _read_json_key(ckey)
        if isinstance(rec, dict) and "ok" in rec:
            return bool(rec.get("ok"))
        data = read_bytes_key(key)
        if not data:
            _write_json_key(ckey, {"ok": False, "ts": datetime.utcnow().isoformat()})
            return False
        try:
            img = Image.open(BytesIO(data))
        except Exception:
            _write_json_key(ckey, {"ok": False, "ts": datetime.utcnow().isoformat()})
            return False
        try:
            payload = detect_signature(img, payload_len_bytes=PAYLOAD_LEN)
            ok = bool(payload)
        except Exception:
            ok = False
        _write_json_key(ckey, {"ok": ok, "ts": datetime.utcnow().isoformat()})
        return ok
    except Exception:
        return False


def _make_item_from_key(uid: str, key: str) -> dict:
    if not key.startswith(f"users/{uid}/"):
        raise ValueError("forbidden key")
    name = os.path.basename(key)
    if s3 and R2_BUCKET:
        url = _get_url_for_key(key, expires_in=60 * 60)
    else:
        url = f"/static/{key}"
    item = {"key": key, "url": url, "name": name}
    # Attach invisible watermark flag (cached)
    try:
        item["has_invisible"] = _has_invisible_mark(uid, key)
    except Exception:
        item["has_invisible"] = False
    return item


def _vault_key(uid: str, vault: str) -> Tuple[str, str]:
    safe = "".join(c for c in vault if c.isalnum() or c in ("-", "_", " ")).strip().replace(" ", "_")
    if not safe:
        raise ValueError("invalid vault name")
    return f"users/{uid}/vaults/{safe}.json", safe


def _vault_meta_key(uid: str, vault: str) -> str:
    _, safe = _vault_key(uid, vault)
    return f"users/{uid}/vaults/_meta/{safe}.json"


def _write_json_key(key: str, payload: dict):
    data = json.dumps(payload, ensure_ascii=False)
    if s3 and R2_BUCKET:
        bucket = s3.Bucket(R2_BUCKET)
        bucket.put_object(Key=key, Body=data.encode('utf-8'), ContentType='application/json', ACL='private')
    else:
        path = os.path.join(STATIC_DIR, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(data)


from botocore.exceptions import ClientError

def _read_json_key(key: str) -> Optional[dict]:
    try:
        if s3 and R2_BUCKET:
            obj = s3.Object(R2_BUCKET, key)
            try:
                body = obj.get()["Body"].read().decode("utf-8")
            except ClientError as ce:
                if ce.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
                    return None
                raise
            return json.loads(body)
        else:
            path = os.path.join(STATIC_DIR, key)
            if not os.path.isfile(path):
                return None
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as ex:
        logger.warning(f"_read_json_key failed for {key}: {ex}")
        return None


def _read_vault(uid: str, vault: str) -> list[str]:
    key, _ = _vault_key(uid, vault)
    try:
        if s3 and R2_BUCKET:
            obj = s3.Object(R2_BUCKET, key)
            try:
                body = obj.get()["Body"].read().decode("utf-8")
            except ClientError as ce:
                # Treat missing object as empty vault without warning noise
                if ce.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
                    return []
                raise
            data = json.loads(body)
        else:
            path = os.path.join(STATIC_DIR, key)
            if not os.path.isfile(path):
                return []
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        return list(data.get("keys", []))
    except Exception as ex:
        logger.warning(f"_read_vault failed for {key}: {ex}")
        return []


def _write_vault(uid: str, vault: str, keys: list[str]):
    key, _ = _vault_key(uid, vault)
    payload = json.dumps({"keys": sorted(set(keys))})
    if s3 and R2_BUCKET:
        bucket = s3.Bucket(R2_BUCKET)
        bucket.put_object(Key=key, Body=payload.encode("utf-8"), ContentType="application/json", ACL="private")
    else:
        path = os.path.join(STATIC_DIR, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)


def _delete_vault(uid: str, vault: str) -> bool:
    try:
        key, safe = _vault_key(uid, vault)
        meta_key = _vault_meta_key(uid, vault)
        if s3 and R2_BUCKET:
            bucket = s3.Bucket(R2_BUCKET)
            to_delete = [{"Key": key}, {"Key": meta_key}]
            bucket.delete_objects(Delete={"Objects": to_delete})
        else:
            path = os.path.join(STATIC_DIR, key)
            meta_path = os.path.join(STATIC_DIR, meta_key)
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass
            try:
                if os.path.isfile(meta_path):
                    os.remove(meta_path)
            except Exception:
                pass
        return True
    except Exception as ex:
        logger.warning(f"_delete_vault failed for {vault}: {ex}")
        return False


_unlocked_vaults: dict[str, set[str]] = {}

def _read_vault_meta(uid: str, vault: str) -> dict:
    key = _vault_meta_key(uid, vault)
    meta = _read_json_key(key)
    return meta or {}


def _write_vault_meta(uid: str, vault: str, meta: dict):
    key = _vault_meta_key(uid, vault)
    _write_json_key(key, meta or {})


def _vault_salt(uid: str, vault: str) -> str:
    return f"{uid}::{vault}::v1"


import hashlib

def _hash_password_legacy(pw: str, salt: str) -> str:
    try:
        return hashlib.sha256(((pw or '') + salt).encode('utf-8')).hexdigest()
    except Exception:
        return ''

def _hash_password_bcrypt(pw: str) -> str:
    try:
        return bcrypt.hashpw((pw or '').encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    except Exception:
        return ''

def _check_password(pw: str, meta: dict, uid: str, vault: str) -> bool:
    try:
        ph = str(meta.get('password_hash') or '')
        if ph:
            try:
                return bcrypt.checkpw((pw or '').encode('utf-8'), ph.encode('utf-8'))
            except Exception:
                return False
        legacy = str(meta.get('hash') or '')
        if legacy:
            salt = _vault_salt(uid, vault)
            return _hash_password_legacy(pw or '', salt) == legacy
    except Exception:
        return False
    return False


def _is_vault_unlocked(uid: str, vault: str) -> bool:
    meta = _read_vault_meta(uid, vault)
    if not meta.get('protected'):
        return True
    s = _unlocked_vaults.get(uid) or set()
    return (vault in s)


def _unlock_vault(uid: str, vault: str, password: str) -> bool:
    meta = _read_vault_meta(uid, vault)
    if not meta.get('protected'):
        return True
    if _check_password(password or '', meta, uid, vault):
        s = _unlocked_vaults.get(uid)
        if not s:
            s = set()
            _unlocked_vaults[uid] = s
        s.add(vault)
        return True
    return False


def _lock_vault(uid: str, vault: str):
    s = _unlocked_vaults.get(uid)
    if s and vault in s:
        s.remove(vault)


@router.get("/vaults")
async def vaults_list(request: Request):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # List vaults by scanning directory/objects
    prefix = f"users/{uid}/vaults/"
    results: list[dict] = []
    try:
        if s3 and R2_BUCKET:
            bucket = s3.Bucket(R2_BUCKET)
            names: list[str] = []
            for obj in bucket.objects.filter(Prefix=prefix):
                key = obj.key
                if not key.endswith(".json"):
                    continue
                # Only consider top-level vault JSON files; skip subdirectories like _meta/, _approvals/, etc.
                tail = key[len(prefix):]
                if "/" in tail:
                    continue
                base = os.path.basename(key)[:-5]
                names.append(base)
            for n in sorted(set(names)):
                keys_list = _read_vault(uid, n)
                if n == FRIENDS_VAULT_SAFE:
                    try:
                        filtered = [k for k in keys_list if ('/partners/' not in k and '-fromfriend' not in os.path.basename(k))]
                    except Exception:
                        filtered = [k for k in keys_list if '/partners/' not in k]
                    count = len(filtered)
                else:
                    count = len(keys_list)
                results.append({"name": n, "count": count})
        else:
            dir_path = os.path.join(STATIC_DIR, prefix)
            if os.path.isdir(dir_path):
                for f in os.listdir(dir_path):
                    if f.endswith(".json") and f != "_meta.json":
                        name = f[:-5]
                        keys_list = _read_vault(uid, name)
                        if name == FRIENDS_VAULT_SAFE:
                            try:
                                filtered = [k for k in keys_list if ('/partners/' not in k and '-fromfriend' not in os.path.basename(k))]
                            except Exception:
                                filtered = [k for k in keys_list if '/partners/' not in k]
                            count = len(filtered)
                        else:
                            count = len(keys_list)
                        results.append({"name": name, "count": count})
    except Exception as ex:
        logger.warning(f"_list_vaults failed: {ex}")
    # Mark protection state and attach display name
    for v in results:
        name = v.get("name")
        if not isinstance(name, str):
            continue
        meta = _read_vault_meta(uid, name)
        v["protected"] = bool(meta.get("protected"))
        v["unlocked"] = _is_vault_unlocked(uid, name)
        try:
            dn = meta.get("display_name") if isinstance(meta, dict) else None
            v["display_name"] = str(dn or name.replace("_", " "))
        except Exception:
            v["display_name"] = name
    return {"vaults": results}


@router.post("/vaults/delete")
async def vaults_delete(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    vaults = payload.get("vaults", [])
    password = str(payload.get("password", "") or "").strip()
    permanent = payload.get("permanent", False)  # If true, skip trash and delete permanently
    
    if not vaults or not isinstance(vaults, list):
        return JSONResponse({"error": "No vaults provided"}, status_code=400)
    
    deleted: list[str] = []
    errors: list[str] = []
    
    for v in vaults:
        name = str(v or '').strip()
        if not name:
            continue
        
        # Check if vault is protected and validate password
        try:
            meta = _read_vault_meta(uid, name)
            if meta.get("protected"):
                # Verify password for protected vaults
                if not password:
                    errors.append(name)
                    continue
                
                if not _check_password(password or '', meta, uid, name):
                    return JSONResponse({"error": "Invalid password"}, status_code=403)
        except Exception:
            pass
        
        # Soft-delete: Move to trash instead of permanent deletion
        if not permanent:
            try:
                from models.vault_trash import VaultTrash
                from datetime import timedelta
                
                keys = _read_vault(uid, name)
                meta = _read_vault_meta(uid, name) or {}
                _, safe_name = _vault_key(uid, name)
                
                # Calculate total size from gallery assets
                total_size = db.query(func.sum(GalleryAsset.size_bytes)).filter(
                    GalleryAsset.user_uid == uid,
                    GalleryAsset.key.in_(keys)
                ).scalar() or 0
                
                # Create trash entry
                trash_entry = VaultTrash(
                    owner_uid=uid,
                    vault_name=safe_name,
                    display_name=meta.get("display_name") or name.replace("_", " "),
                    original_keys=keys,
                    vault_metadata=meta,
                    photo_count=len(keys),
                    total_size_bytes=total_size,
                    expires_at=datetime.utcnow() + timedelta(days=30)
                )
                db.add(trash_entry)
                db.commit()
            except Exception as ex:
                logger.warning(f"Failed to create trash entry for {name}: {ex}")
                try:
                    db.rollback()
                except:
                    pass
        
        ok = _delete_vault(uid, name)
        if ok:
            deleted.append(name)
        else:
            errors.append(name)
    
    return {"deleted": deleted, "errors": errors, "movedToTrash": not permanent}


@router.get("/vaults/chat/token")
async def get_sendbird_token(request: Request):
    """Get Sendbird access token for the current user"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        user_email = get_user_email_from_uid(uid)
        if not user_email:
            return JSONResponse({"error": "User email not found"}, status_code=400)
        
        # Ensure user exists in Sendbird and get access token
        access_token = await ensure_sendbird_user(uid, user_email)
        if not access_token:
            return JSONResponse({"error": "Failed to get Sendbird token"}, status_code=500)
        
        return {
            "access_token": access_token,
            "user_id": uid,
            "nickname": user_email
        }
    except Exception as ex:
        logger.error(f"Failed to get Sendbird token: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/vaults/chat/channel")
async def get_vault_channel(request: Request, vault: str):
    """Get Sendbird channel URL for a vault"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        meta = _read_vault_meta(uid, vault)
        channel_url = meta.get("channel_url") if meta else None
        
        if not channel_url:
            return JSONResponse({"error": "No chat channel found for this vault"}, status_code=404)
        
        return {"channel_url": channel_url}
    except Exception as ex:
        logger.error(f"Failed to get vault channel: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


class VaultCreatePayload(BaseModel):
    name: str
    protect: Optional[bool] = False
    password: Optional[str] = None
    client_emails: Optional[List[str]] = []

@router.post("/vaults/create")
async def vaults_create(request: Request, payload: VaultCreatePayload, db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        name = payload.name
        keys = _read_vault(uid, name)
        _write_vault(uid, name, keys)
        
        # Initialize vault metadata
        meta = {}
        if payload.protect and (payload.password or '').strip():
            salt = _vault_salt(uid, name)
            meta.update({"protected": True, "hash": _hash_password(payload.password or '', salt)})
        
        # Create Sendbird channel for vault communication
        channel_url = None
        if payload.client_emails:
            try:
                # Get photographer email
                photographer_email = get_user_email_from_uid(uid)
                if photographer_email:
                    # Ensure photographer exists in Sendbird
                    await ensure_sendbird_user(uid, photographer_email)
                    
                    # Create client user IDs from emails (you may want to map these differently)
                    client_ids = [email.replace('@', '_at_').replace('.', '_dot_') for email in payload.client_emails]
                    
                    # Ensure clients exist in Sendbird
                    for i, email in enumerate(payload.client_emails):
                        await ensure_sendbird_user(client_ids[i], email)
                    
                    # Create vault channel
                    channel_url = await create_vault_channel(name, uid, client_ids)
                    if channel_url:
                        meta["channel_url"] = channel_url
                        logger.info(f"Created Sendbird channel for vault {name}: {channel_url}")
            except Exception as ex:
                logger.warning(f"Failed to create Sendbird channel for vault {name}: {ex}")
                # Don't fail vault creation if chat setup fails
        
        # Save metadata if any
        if meta:
            _write_vault_meta(uid, name, meta)
        try:
            safe_name = _vault_key(uid, name)[1]
            _pg_upsert_vault_meta(db, uid, safe_name, meta if meta else {}, visibility="private")
        except Exception:
            pass
        
        return {
            "name": _vault_key(uid, name)[1], 
            "count": len(keys),
            "channel_url": channel_url
        }
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.post("/vaults/add")
async def vaults_add(request: Request, vault: str = Body(..., embed=True), keys: List[str] = Body(..., embed=True), password: Optional[str] = Body(None, embed=True), db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Owner always has access to their own vaults, no password needed
    try:
        exist = _read_vault(uid, vault)
        filtered = [k for k in keys if k.startswith(f"users/{uid}/")]
        merged = sorted(set(exist) | set(filtered))
        _write_vault(uid, vault, merged)
        try:
            safe_vault = _vault_key(uid, vault)[1]
            _pg_upsert_vault_meta(db, uid, safe_vault, {}, visibility="private")
        except Exception:
            pass
        return {"vault": _vault_key(uid, vault)[1], "count": len(merged)}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.post("/vaults/upload")
async def vaults_upload(
    request: Request,
    files: List[UploadFile] = File(...),
    vault: str = Form(...),
    password: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """Upload files directly to a vault (not to general uploads area)"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # Check vault exists or create it
    try:
        _read_vault_meta(uid, vault)
    except:
        # Vault doesn't exist, create it
        try:
            _write_vault_meta(uid, vault, {})
            _write_vault(uid, vault, [])
        except Exception as ex:
            return JSONResponse({"error": f"Failed to create vault: {str(ex)}"}, status_code=400)
    
    # Owner always has access to their own vaults, no password needed
    
    if not files:
        return JSONResponse({"error": "No files provided"}, status_code=400)
    
    # Rate limiting and validation
    from utils.rate_limit import check_upload_rate_limit, validate_upload_request, validate_file_size, is_video_file
    
    # Check if batch contains videos for appropriate limits
    has_videos = any(is_video_file(f.filename or '') for f in files)
    valid, err_msg = validate_upload_request(len(files), 0, has_videos=has_videos)
    if not valid:
        return JSONResponse({"error": err_msg}, status_code=400)
    
    allowed, rate_err = check_upload_rate_limit(uid, file_count=len(files))
    if not allowed:
        return JSONResponse({"error": rate_err}, status_code=429)
    
    # Allowed extensions for images and videos
    IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.tif', '.tiff', '.gif', '.cr2', '.cr3', '.nef', '.nrw', '.arw', '.sr2', '.srf', '.srw', '.orf', '.raf', '.rw2', '.rwl', '.pef', '.dng', '.3fr', '.erf', '.kdc', '.mrw', '.x3f', '.mef', '.iiq', '.fff'}
    VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.wmv', '.flv', '.3gp', '.mpeg', '.mpg', '.mts', '.m2ts'}
    ALLOWED_EXTS = IMAGE_EXTS | VIDEO_EXTS
    
    uploaded = []
    errors = []
    
    for uf in files:
        try:
            raw = await uf.read()
            if not raw:
                continue
            
            # Validate file size (handles both image and video limits)
            file_valid, file_err = validate_file_size(len(raw), uf.filename or '')
            if not file_valid:
                errors.append({"filename": uf.filename, "error": file_err})
                continue
            
            # Determine file extension
            orig_filename = uf.filename or 'image.jpg'
            ext = os.path.splitext(orig_filename)[1].lower()
            if not ext or ext not in ALLOWED_EXTS:
                ext = '.jpg'
            
            # Auto-embed IPTC/EXIF metadata if user has it enabled
            try:
                raw = auto_embed_metadata_for_user(raw, uid)
            except Exception as meta_ex:
                logger.debug(f"Metadata embed skipped: {meta_ex}")
            
            # Generate unique key in user's vault space
            ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            random_suffix = secrets.token_hex(4)
            safe_filename = "".join(c for c in orig_filename if c.isalnum() or c in ('-', '_', '.')).replace(' ', '_')[:50]
            safe_filename = os.path.splitext(safe_filename)[0]  # Remove extension
            key = f"users/{uid}/vaults/{vault}/{ts}_{random_suffix}_{safe_filename}{ext}"
            
            # Upload to R2
            content_type = uf.content_type or 'image/jpeg'
            if s3 and R2_BUCKET:
                s3.Object(R2_BUCKET, key).put(Body=raw, ContentType=content_type)
            else:
                # Fallback to local storage
                local_path = os.path.join(STATIC_DIR, key)
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, 'wb') as f:
                    f.write(raw)
            
            uploaded.append({
                "key": key,
                "filename": orig_filename,
                "size": len(raw)
            })
            try:
                existing = db.query(GalleryAsset).filter(GalleryAsset.key == key).first()
                if existing:
                    existing.user_uid = uid
                    existing.vault = _vault_key(uid, vault)[1]
                    existing.size_bytes = len(raw)
                else:
                    rec = GalleryAsset(
                        user_uid=uid,
                        vault=_vault_key(uid, vault)[1],
                        key=key,
                        size_bytes=len(raw),
                    )
                    db.add(rec)
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            
        except Exception as ex:
            logger.error(f"Failed to upload file {uf.filename}: {ex}")
            errors.append({"filename": uf.filename, "error": str(ex)})
    
    if not uploaded:
        return JSONResponse({"error": "No files uploaded successfully", "errors": errors}, status_code=400)
    
    # Add uploaded keys to vault
    try:
        exist = _read_vault(uid, vault)
        new_keys = [item["key"] for item in uploaded]
        merged = sorted(set(exist) | set(new_keys))
        _write_vault(uid, vault, merged)
    except Exception as ex:
        logger.error(f"Failed to add keys to vault: {ex}")
        return JSONResponse({"error": f"Files uploaded but failed to add to vault: {str(ex)}"}, status_code=500)
    
    return {
        "uploaded": uploaded,
        "vault": vault,
        "count": len(uploaded),
        "errors": errors if errors else None
    }


@router.post("/vaults/remove")
async def vaults_remove(request: Request, vault: str = Body(..., embed=True), keys: List[str] = Body(..., embed=True), password: Optional[str] = Body(None, embed=True), delete_from_r2: Optional[bool] = Body(False, embed=True), db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Owner always has access to their own vaults, no password needed
    try:
        exist = _read_vault(uid, vault)
        to_remove = set(keys)
        
        # Auto-create snapshot before bulk removal (if removing 5+ photos)
        if len(to_remove) >= 5:
            try:
                _, safe_vault = _vault_key(uid, vault)
                meta = _read_vault_meta(uid, vault) or {}
                max_ver = db.query(func.max(VaultVersion.version_number)).filter(
                    VaultVersion.owner_uid == uid,
                    VaultVersion.vault_name == safe_vault
                ).scalar() or 0
                
                snapshot = VaultVersion(
                    owner_uid=uid,
                    vault_name=safe_vault,
                    version_number=max_ver + 1,
                    snapshot_keys=exist,
                    vault_metadata=meta,
                    photo_count=len(exist),
                    description=f"Auto-backup before removing {len(to_remove)} photos"
                )
                db.add(snapshot)
                db.commit()
            except Exception as snap_ex:
                logger.warning(f"Failed to create auto-snapshot: {snap_ex}")
                try:
                    db.rollback()
                except:
                    pass
        
        remain = [k for k in exist if k not in to_remove]
        _write_vault(uid, vault, remain)
        try:
            if to_remove:
                db.query(GalleryAsset).filter(GalleryAsset.user_uid == uid, GalleryAsset.key.in_(list(to_remove))).delete(synchronize_session=False)
                db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

        deleted: list[str] = []
        errors: list[str] = []
        if delete_from_r2 and to_remove:
            # Only delete keys belonging to this user for safety
            allowed = [k for k in to_remove if k.startswith(f"users/{uid}/")]
            if allowed:
                if s3 and R2_BUCKET:
                    try:
                        bucket = s3.Bucket(R2_BUCKET)
                        objs = [{"Key": k} for k in allowed]
                        resp = bucket.delete_objects(Delete={"Objects": objs, "Quiet": False})
                        for d in resp.get("Deleted", []):
                            k = d.get("Key")
                            if k:
                                deleted.append(k)
                        for e in resp.get("Errors", []):
                            errors.append(f"{e.get('Key') or ''}: {e.get('Message') or str(e)}")
                    except Exception as ex:
                        logger.exception(f"Vault remove delete error: {ex}")
                        errors.append(str(ex))
                else:
                    # Local filesystem
                    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))
                    for k in allowed:
                        path = os.path.join(base, k)
                        try:
                            if os.path.exists(path):
                                os.remove(path)
                                deleted.append(k)
                        except Exception as _ex:
                            errors.append(f"{k}: {str(_ex)}")

    except Exception as ex:
        logger.exception(f"Vaults remove error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=400)

    return {"deleted": deleted, "errors": errors}


class LicenseUpdatePayload(BaseModel):
    vault: str
    price_cents: int
    currency: str = "USD"


@router.get("/vaults/license")
async def vaults_get_license(request: Request, vault: str):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        safe_vault = _vault_key(uid, vault)[1]
        meta = _read_vault_meta(uid, safe_vault) or {}
        return {
            "vault": safe_vault,
            "price_cents": int(meta.get("license_price_cents") or 0),
            "currency": str(meta.get("license_currency") or "USD"),
        }
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.post("/vaults/license")
async def vaults_set_license(request: Request, payload: LicenseUpdatePayload):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    v = (payload.vault or '').strip()
    if not v:
        return JSONResponse({"error": "vault required"}, status_code=400)
    if payload.price_cents is None or payload.price_cents < 0:
        return JSONResponse({"error": "price_cents must be >= 0"}, status_code=400)
    currency = (payload.currency or 'USD').upper()
    try:
        safe_vault = _vault_key(uid, v)[1]
        meta = _read_vault_meta(uid, safe_vault) or {}
        meta["license_price_cents"] = int(payload.price_cents)
        meta["license_currency"] = currency
        _write_vault_meta(uid, safe_vault, meta)
        return {"ok": True, "vault": safe_vault, "price_cents": meta["license_price_cents"], "currency": meta["license_currency"]}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)



class VaultMetaUpdate(BaseModel):
    vault: str
    display_name: Optional[str] | None = None
    order: Optional[list[str]] | None = None
    share_hide_ui: Optional[bool] | None = None
    share_color: Optional[str] | None = None
    share_layout: Optional[str] | None = None  # 'grid' | 'masonry'
    share_logo_url: Optional[str] | None = None
    share_bg_color: Optional[str] | None = None
    descriptions: Optional[dict[str, str]] | None = None


class SlideshowItem(BaseModel):
    key: str
    title: Optional[str] = None


class SlideshowUpdatePayload(BaseModel):
    vault: str
    slideshow: List[SlideshowItem]


@router.post("/vaults/meta")
async def vaults_set_meta(request: Request, payload: VaultMetaUpdate, db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    v = (payload.vault or '').strip()
    if not v:
        return JSONResponse({"error": "vault required"}, status_code=400)
    try:
        safe_vault = _vault_key(uid, v)[1]
        meta = _read_vault_meta(uid, safe_vault) or {}
        if payload.display_name is not None:
            meta["display_name"] = str(payload.display_name).strip()
        # Optional persisted order
        if isinstance(payload.order, list):
            existing = set(_read_vault(uid, safe_vault))
            clean = [k for k in payload.order if isinstance(k, str) and k in existing]
            meta["order"] = clean
        # Share customization
        if payload.share_hide_ui is not None:
            meta["share_hide_ui"] = bool(payload.share_hide_ui)
        if payload.share_color is not None:
            meta["share_color"] = str(payload.share_color).strip()
        if payload.share_layout is not None:
            lay = (payload.share_layout or 'grid').strip().lower()
            if lay not in ("grid", "masonry"):
                lay = "grid"
            meta["share_layout"] = lay
        if payload.share_logo_url is not None:
            meta["share_logo_url"] = str(payload.share_logo_url).strip()
        if payload.share_bg_color is not None:
            meta["share_bg_color"] = str(payload.share_bg_color).strip()
        if isinstance(payload.descriptions, dict):
            # Merge into existing descriptions map
            existing_desc = meta.get("descriptions") or {}
            if not isinstance(existing_desc, dict):
                existing_desc = {}
            clean_desc: dict[str, str] = {}
            for k, v in payload.descriptions.items():
                try:
                    ks = str(k).strip()
                    vs = str(v).strip()
                    if ks and vs:
                        clean_desc[ks] = vs
                except Exception:
                    continue
            existing_desc.update(clean_desc)
            meta["descriptions"] = existing_desc
        _write_vault_meta(uid, safe_vault, meta)
        _pg_upsert_vault_meta(db, uid, safe_vault, {
            "display_name": meta.get("display_name"),
            "order": meta.get("order"),
            "share_hide_ui": meta.get("share_hide_ui"),
            "share_color": meta.get("share_color"),
            "share_layout": meta.get("share_layout"),
            "share_logo_url": meta.get("share_logo_url"),
            "share_bg_color": meta.get("share_bg_color"),
            "descriptions": meta.get("descriptions"),
        })
        return {"ok": True, "vault": safe_vault, "display_name": meta.get("display_name"), "order": meta.get("order"), "share": {
            "hide_ui": bool(meta.get("share_hide_ui")),
            "color": str(meta.get("share_color") or ""),
            "layout": str(meta.get("share_layout") or "grid"),
            "logo_url": str(meta.get("share_logo_url") or ""),
        }}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.post("/vaults/customize")
async def vaults_customize(request: Request, vault: str = Body(..., embed=True), db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    v = (vault or '').strip()
    if not v:
        return JSONResponse({"error": "vault required"}, status_code=400)
    try:
        safe_vault = _vault_key(uid, v)[1]
        mmeta = _pg_read_vault_meta(db, uid, safe_vault) or _read_vault_meta(uid, safe_vault) or {}
        share = {
            "hide_ui": bool(mmeta.get("share_hide_ui")),
            "color": str(mmeta.get("share_color") or ""),
            "layout": str(mmeta.get("share_layout") or "grid"),
            "logo_url": str(mmeta.get("share_logo_url") or ""),
            "welcome_message": str(mmeta.get("welcome_message") or ""),
            "bg_color": str(mmeta.get("share_bg_color") or ""),
        }
        return {"ok": True, "vault": safe_vault, "display_name": str(mmeta.get("display_name") or safe_vault), "share": share}
    except Exception as ex:
        logger.warning(f"/vaults/customize failed: {ex}")
        return JSONResponse({"error": "load_failed"}, status_code=500)


@router.post("/vaults/unlock")
async def vaults_unlock(request: Request, vault: str = Body(..., embed=True), password: str = Body(..., embed=True)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if _unlock_vault(uid, vault, password or ''):
        return {"ok": True}
    return JSONResponse({"error": "Invalid password"}, status_code=403)


@router.post("/vaults/lock")
async def vaults_lock(request: Request, vault: str = Body(..., embed=True)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    _lock_vault(uid, vault)
    return {"ok": True}


class VaultProtectionPayload(BaseModel):
    vault: str
    protect: bool
    password: Optional[str] = None


@router.post("/vaults/update-protection")
async def vaults_update_protection(request: Request, payload: VaultProtectionPayload):
    """Update vault protection settings (add or remove password protection)"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    vault = (payload.vault or '').strip()
    if not vault:
        return JSONResponse({"error": "vault required"}, status_code=400)
    
    try:
        # Get the safe vault name
        safe_vault = _vault_key(uid, vault)[1]
        
        # Read existing metadata
        meta = _read_vault_meta(uid, safe_vault) or {}
        
        if payload.protect:
            # Adding protection
            if not payload.password:
                return JSONResponse({"error": "password required for protection"}, status_code=400)
            meta["protected"] = True
            meta["password_hash"] = _hash_password_bcrypt(payload.password)
            # Lock the vault after adding protection
            _lock_vault(uid, safe_vault)
        else:
            # Removing protection
            meta["protected"] = False
            if "hash" in meta:
                del meta["hash"]
            if "password_hash" in meta:
                del meta["password_hash"]
            
            # Unlock the vault after removing protection
            _lock_vault(uid, safe_vault)
        
        # Save updated metadata
        _write_vault_meta(uid, safe_vault, meta)
        
        return {
            "ok": True,
            "vault": safe_vault,
            "protected": bool(meta.get("protected"))
        }
    except Exception as ex:
        logger.error(f"Failed to update vault protection: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=400)


def _get_thumbnail_url_fast(key_or_url: str, expires_in: int = 3600) -> Optional[str]:
    """Get thumbnail URL - uses Cloudinary for Cloudinary URLs, S3 thumbnails for storage keys (fast check)."""
    try:
        # If it's a Cloudinary URL, use Cloudinary thumbnail transformation
        if 'cloudinary.com' in key_or_url:
            from utils.cloudinary import get_cloudinary_thumbnail_url
            return get_cloudinary_thumbnail_url(key_or_url)
        
        # Otherwise, treat as S3/R2 storage key and check for generated thumbnail
        from utils.thumbnails import get_thumbnail_key
        thumb_key = get_thumbnail_key(key_or_url, 'small')
        if s3 and R2_BUCKET:
            try:
                s3.Object(R2_BUCKET, thumb_key).load()
                return _get_url_for_key(thumb_key, expires_in=expires_in)
            except Exception:
                return None
        return None
    except Exception:
        return None


def _make_item_fast(uid: str, key: str) -> dict:
    """Fast item creation without invisible watermark detection - for gallery view"""
    if not key.startswith(f"users/{uid}/"):
        raise ValueError("forbidden key")
    name = os.path.basename(key)
    if s3 and R2_BUCKET:
        url = _get_url_for_key(key, expires_in=60 * 60)
        thumb_url = _get_thumbnail_url_fast(key, expires_in=60 * 60)
    else:
        url = f"/static/{key}"
        thumb_url = None
    return {"key": key, "url": url, "thumb_url": thumb_url, "name": name}


@router.get("/vaults/photos")
async def vaults_photos(request: Request, vault: str, password: Optional[str] = None, limit: Optional[int] = None, cursor: Optional[str] = None, fast: Optional[bool] = True):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Owner always has access to their own vaults, no password needed
    # Password protection only applies to shared access via tokens
    try:
        all_keys = _read_vault(uid, vault)
        total_count = len(all_keys)
        
        start_index = 0
        if cursor:
            try:
                start_index = max(0, int(cursor))
            except Exception:
                start_index = 0
        eff_limit = None
        if isinstance(limit, int) and limit > 0:
            eff_limit = max(1, min(limit, 1000))
        
        keys = all_keys
        try:
            if vault == FRIENDS_VAULT_SAFE:
                keys = [k for k in keys if ('/partners/' not in k and '-fromfriend' not in os.path.basename(k))]
                total_count = len(keys)
        except Exception:
            pass
        
        # Apply optional explicit order from meta if present
        try:
            vmeta = _read_vault_meta(uid, vault)
            order = vmeta.get("order") if isinstance(vmeta, dict) else None
            if isinstance(order, list) and order:
                order_index = {k: i for i, k in enumerate(order)}
                keys = sorted(keys, key=lambda k: order_index.get(k, 10**9))
        except Exception:
            pass
        
        # Apply pagination slice
        if eff_limit is not None or cursor:
            keys = keys[start_index : (start_index + (eff_limit or len(keys)))]

        # Filter out JSON sidecars early
        keys = [k for k in keys if not k.lower().endswith('.json')]

        items: list[dict] = []
        
        # FAST MODE: Skip expensive originals lookup and invisible watermark detection
        # This makes initial vault load much faster (like SmugMug/Pixieset)
        if fast:
            for key in keys:
                try:
                    item = _make_item_fast(uid, key)
                    items.append(item)
                except Exception:
                    pass
        else:
            # FULL MODE: Include originals lookup (for download/export features)
            if s3 and R2_BUCKET:
                # Only look up originals for the specific keys we need, not ALL originals
                for key in keys:
                    try:
                        item = _make_item_from_key(uid, key)
                        name = os.path.basename(key)
                        
                        # Try to find original for this specific photo
                        if "-o" in name:
                            try:
                                base_part = name.rsplit("-o", 1)[0]
                                for suf in ("-logo", "-txt"):
                                    if base_part.endswith(suf):
                                        base_part = base_part[: -len(suf)]
                                        break
                                dir_part = os.path.dirname(key)
                                date_part = "/".join(dir_part.split("/")[-3:])
                                for ext in ("jpg","jpeg","png","webp","heic","tif","tiff","bin"):
                                    cand = f"users/{uid}/originals/{date_part}/{base_part}-orig.{ext}" if ext != 'bin' else f"users/{uid}/originals/{date_part}/{base_part}-orig.bin"
                                    # Check if original exists (single HEAD request is faster than listing all)
                                    try:
                                        s3.Object(R2_BUCKET, cand).load()
                                        item["original_key"] = cand
                                        item["original_url"] = _get_url_for_key(cand, expires_in=60 * 60)
                                        break
                                    except Exception:
                                        continue
                            except Exception:
                                pass
                        
                        # Attach optional friend note metadata if exists
                        try:
                            if "-fromfriend-" in name:
                                meta_key = f"{os.path.splitext(key)[0]}.json"
                                fmeta = read_json_key(meta_key)
                                if isinstance(fmeta, dict) and (fmeta.get("note") or fmeta.get("from")):
                                    item["friend_note"] = str(fmeta.get("note") or "")
                                    if fmeta.get("from"):
                                        item["friend_from"] = str(fmeta.get("from"))
                                    if fmeta.get("at"):
                                        item["friend_at"] = str(fmeta.get("at"))
                        except Exception:
                            pass
                        items.append(item)
                    except Exception:
                        pass
            else:
                # Local storage fallback
                for key in keys:
                    try:
                        item = _make_item_from_key(uid, key)
                        items.append(item)
                    except Exception:
                        pass
        
        result = {"photos": items, "total": total_count}
        if eff_limit is not None:
            next_start = start_index + eff_limit
            if next_start < total_count:
                result["next_cursor"] = str(next_start)
        return result
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.post("/vaults/preview/generate")
async def vaults_generate_preview(request: Request, vault: str = Body(..., embed=True)):
    """Generate a public preview link for a vault"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    vault = str(vault or '').strip()
    if not vault:
        return JSONResponse({"error": "vault required"}, status_code=400)
    
    try:
        # Validate vault exists
        safe_vault = _vault_key(uid, vault)[1]
        keys = _read_vault(uid, safe_vault)
        if not keys:
            return JSONResponse({"error": "Vault is empty"}, status_code=400)
        
        # Generate preview token
        token = secrets.token_urlsafe(24)
        now = datetime.utcnow()
        
        # Preview links don't expire by default (or set a very long expiry)
        preview_rec = {
            "token": token,
            "uid": uid,
            "vault": safe_vault,
            "type": "preview",
            "created_at": now.isoformat(),
        }
        
        # Store preview token
        preview_key = f"previews/{token}.json"
        _write_json_key(preview_key, preview_rec)
        
        # Generate preview URL
        front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
        preview_url = f"{front}/preview/{token}"
        
        return {
            "token": token,
            "url": preview_url,
            "vault": safe_vault,
            "photo_count": len(keys)
        }
    except Exception as ex:
        logger.error(f"Failed to generate preview: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.get("/vaults/preview/{token}")
async def vaults_get_preview(token: str, limit: Optional[int] = None, cursor: Optional[str] = None, db: Session = Depends(get_db)):
    """Get vault photos using preview token (public, no auth required)"""
    try:
        preview_key = f"previews/{token}.json"
        preview_rec = _read_json_key(preview_key)
        
        if not preview_rec:
            return JSONResponse({"error": "Invalid preview token"}, status_code=404)
        
        uid = preview_rec.get("uid")
        vault = preview_rec.get("vault")
        
        if not uid or not vault:
            return JSONResponse({"error": "Invalid preview data"}, status_code=400)
        
        # Get vault photos
        keys = _read_vault(uid, vault)
        start_index = 0
        if cursor:
            try:
                start_index = max(0, int(cursor))
            except Exception:
                start_index = 0
        eff_limit = None
        if isinstance(limit, int) and limit > 0:
            eff_limit = max(1, min(limit, 1000))
        if eff_limit is not None or cursor:
            keys = keys[start_index : (start_index + (eff_limit or len(keys)))]
        items = []
        for k in keys:
            try:
                item = _make_item_from_key(uid, k)
                items.append(item)
            except Exception:
                pass
        
        # Get vault metadata
        meta = _read_vault_meta(uid, vault)
        display_name = meta.get("display_name") if meta else None
        
        # Get slideshow data
        slideshow_items = []
        if meta and "slideshow" in meta:
            slideshow_data = meta["slideshow"]
            for slide in slideshow_data:
                # Find the photo in items to get the URL
                photo_key = slide.get("key")
                if photo_key:
                    matching_photo = next((item for item in items if item["key"] == photo_key), None)
                    if matching_photo:
                        slideshow_items.append({
                            "key": photo_key,
                            "url": matching_photo["url"],
                            "name": matching_photo["name"],
                            "title": slide.get("title", "")
                        })
        
        # Get brand kit from user
        brand_kit = {}
        try:
            user = db.query(User).filter(User.uid == uid).first()
            if user:
                brand_kit_data = {
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
                brand_kit = {k: v for k, v in brand_kit_data.items() if v is not None}
        except Exception:
            pass
        
        resp = {
            "vault": vault,
            "display_name": display_name or vault.replace("_", " "),
            "photos": items,
            "photo_count": len(items),
            "slideshows": slideshow_items,
            "brand_kit": brand_kit
        }
        if eff_limit is not None:
            next_start = start_index + eff_limit
            all_keys = _read_vault(uid, vault)
            if next_start < len(all_keys):
                resp["next_cursor"] = str(next_start)
        return resp
    except Exception as ex:
        logger.error(f"Failed to get preview: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.post("/vaults/share")
async def vaults_share(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    vault = str((payload or {}).get('vault') or '').strip()
    email = str((payload or {}).get('email') or '').strip()
    client_name = str((payload or {}).get('client_name') or '').strip()
    if not vault or not email:
        return JSONResponse({"error": "vault and email required"}, status_code=400)
    # Validate vault exists and get normalized name
    try:
        keys = _read_vault(uid, vault)
        safe_vault = _vault_key(uid, vault)[1]
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)

    try:
        _ = _read_vault(uid, vault)
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)

    expires_at_str = (payload or {}).get('expires_at')
    expires_in_days = (payload or {}).get('expires_in_days')
    now = datetime.utcnow()
    if expires_at_str:
        try:
            exp = datetime.fromisoformat(expires_at_str.replace('Z', '+00:00'))
            if not exp.tzinfo:
                exp = exp.replace(tzinfo=None)
            expires_at_iso = exp.isoformat()
        except Exception:
            return JSONResponse({"error": "invalid expires_at"}, status_code=400)
    else:
        days = int(expires_in_days or 7)
        exp = now + timedelta(days=days)
        expires_at_iso = exp.isoformat()

    token = secrets.token_urlsafe(24)
    rec = {
        "token": token,
        "uid": uid,
        "vault": _vault_key(uid, vault)[1],
        "email": email.lower(),
        "expires_at": expires_at_iso,
        "used": False,
        "created_at": now.isoformat(),
        "max_uses": 1,
        "client_name": client_name,
    }
    # Granular client download permission
    try:
        perm_raw = str((payload or {}).get('download_permission') or '').strip().lower()
        if perm_raw not in ('proofing', 'proofing_download'):
            perm_raw = 'proofing'
        rec['download_permission'] = perm_raw
    except Exception:
        rec['download_permission'] = 'proofing'
    # Optional client role for share link
    try:
        role_raw = str((payload or {}).get('client_role') or '').strip().lower()
        if role_raw not in ('viewer', 'editor', 'owner'):
            role_raw = 'viewer'
        rec['client_role'] = role_raw
    except Exception:
        rec['client_role'] = 'viewer'
    
    # Download pricing (free or paid)
    try:
        download_type = str((payload or {}).get('download_type') or 'free').strip().lower()
        if download_type not in ('free', 'paid'):
            download_type = 'free'
        rec['download_type'] = download_type
        
        download_price_cents = int((payload or {}).get('download_price_cents') or 0)
        rec['download_price_cents'] = max(0, download_price_cents)
        
        # Set licensed flag: true for free downloads, false for paid (until payment completes)
        if download_type == 'free':
            rec['licensed'] = True
        else:
            rec['licensed'] = False
        
        rec['payment_id'] = None
        rec['payment_completed_at'] = None
    except Exception:
        rec['download_type'] = 'free'
        rec['download_price_cents'] = 0
        rec['licensed'] = True
        rec['payment_id'] = None
        rec['payment_completed_at'] = None
    
    # Download limit (1-50, 0 = unlimited)
    try:
        download_limit = int((payload or {}).get('download_limit') or 0)
        if download_limit < 0:
            download_limit = 0
        elif download_limit > 50:
            download_limit = 50
        rec['download_limit'] = download_limit
        rec['download_count'] = 0
    except Exception:
        rec['download_limit'] = 0
        rec['download_count'] = 0
    
    # Optional: password to unlock removal of invisible watermark (unmarked originals access)
    try:
        remove_pw = str((payload or {}).get('remove_password') or '').strip()
        if remove_pw:
            # Only enable if at least one photo has invisible watermark
            has_any_invisible = False
            try:
                for k in keys[:50]:  # cap detection for performance
                    if _has_invisible_mark(uid, k):
                        has_any_invisible = True
                        break
            except Exception:
                has_any_invisible = False
            if has_any_invisible:
                import hashlib
                salt = f"share::{token}"
                rec["remove_pw_hash"] = hashlib.sha256(((remove_pw or '') + salt).encode('utf-8')).hexdigest()
                rec["remove_pw_required"] = True
    except Exception:
        pass
    _write_json_key(_share_key(token), rec)
    try:
        _pg_upsert_vault_meta(db, uid, safe_vault, {}, visibility="shared")
    except Exception:
        pass

    front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
    
    # Check if vault has a custom domain configured
    custom_domain_link = None
    try:
        from models.vault_domain import VaultDomain
        vault_domain = db.query(VaultDomain).filter(
            VaultDomain.uid == uid,
            VaultDomain.vault_name == safe_vault,
            VaultDomain.enabled == True
        ).first()
        if vault_domain:
            custom_domain_link = f"https://{vault_domain.hostname}"
            # Update the share token in the domain record
            vault_domain.share_token = token
            db.commit()
    except Exception as e:
        logger.debug(f"Custom domain check failed: {e}")
    
    # Use custom domain if available, otherwise use default link
    link = custom_domain_link if custom_domain_link else f"{front}/#share?token={token}"

    include_qr = bool((payload or {}).get('include_qr'))
    qr_bytes = None
    if include_qr:
        try:
            from io import BytesIO
            qr = qrcode.QRCode(version=1, box_size=8, border=2)
            qr.add_data(link)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            qr_bytes = buf.getvalue()
        except Exception:
            qr_bytes = None

    # Compute photo count and pluralize noun
    count = len(keys)
    noun = "photo" if count == 1 else "photos"

    # Resolve photographer/studio name from Firestore
    studio_name = None
    try:
        db = get_fs_client()
        if db:
            doc = db.collection('users').document(uid).get()
            data = doc.to_dict() if getattr(doc, 'exists', False) else {}
            studio_name = (
                data.get('studioName')
                or data.get('studio_name')
                or data.get('businessName')
                or data.get('business_name')
                or data.get('brand_name')
                or data.get('brandName')
                or data.get('displayName')
                or data.get('display_name')
                or data.get('name')
            )
    except Exception:
        studio_name = None
    if not studio_name:
        try:
            owner_email = (get_user_email_from_uid(uid) or '').strip()
            studio_name = (owner_email.split('@')[0] if '@' in owner_email else owner_email) or os.getenv("APP_NAME", "Photomark")
        except Exception:
            studio_name = os.getenv("APP_NAME", "Photomark")

    # Prepare formatted expiry in UTC
    try:
        exp_dt = datetime.fromisoformat(expires_at_iso.replace('Z', ''))
    except Exception:
        exp_dt = exp
    expire_pretty = f"{exp_dt.strftime('%Y-%m-%d at %H:%M')} UTC"

    subject = "Your photos are ready for review 📸"

    client_greeting = f"Hello {client_name}," if client_name else "Hello,"

    # Check if vault is protected and get password info
    vault_meta = _read_vault_meta(uid, safe_vault)
    is_protected = vault_meta.get('protected', False)
    password_info = ""
    password_info_text = ""
    
    if is_protected:
        vault_password = str((payload or {}).get('vault_password') or '').strip()
        if vault_password:
            password_info = (
                f"<br>Vault Password: <strong>{vault_password}</strong>"
            )
            password_info_text = f"\nVault Password: {vault_password}"

    body_html = (
        f"{client_greeting}<br><br>"
        f"Your photographer, {studio_name}, has shared your photos with you for review.<br><br>"
        f"You can securely view {count} {noun}, mark your favorites, approve them, or request changes — all in one place.<br><br>"
        f"👉 View your photos here:<br>"
        f"<a href=\"{link}\">{link}</a><br><br>"
        f"This private link will expire on <strong>{expire_pretty}</strong>.<br><br>"
        f"If you have any questions while reviewing, you can leave feedback directly on the photos.<br><br>"
        f"Enjoy reviewing your images!"
        f"{password_info}"
    )

    extra = ""
    if qr_bytes:
        extra = "<br><br><div><img src=\"cid:share_qr\" alt=\"QR code to open vault\" style=\"max-width:220px;height:auto;border-radius:12px;border:1px solid #333;\" /></div>"
    html = render_email(
        "email_basic.html",
        title="Photos shared for your review",
        intro=(body_html + extra),
        button_label="View photos",
        button_url=link,
        footer_note="If you did not expect this email, you can ignore it.",
    )

    text = (
        (client_greeting.replace('<br>', '').replace('</br>', '').replace('<br/>', ''))
        + "\n\n"
        + f"Your photographer, {studio_name}, has shared your photos with you for review.\n\n"
        + f"You can securely view {count} {noun}, mark your favorites, approve them, or request changes — all in one place.\n\n"
        + "👉 View your photos here:\n"
        + f"{link}\n\n"
        + f"This private link will expire on {expire_pretty}.\n\n"
        + "If you have any questions while reviewing, you can leave feedback directly on the photos.\n\n"
        + "Enjoy reviewing your images!"
        + password_info_text
    )

    attachments = None
    if qr_bytes:
        attachments = [{"filename": "vault-qr.png", "content": qr_bytes, "mime_type": "image/png", "cid": "share_qr"}]
    sent = send_email_smtp(email, subject, html, text, attachments=attachments)
    if not sent:
        logger.error("Failed to send share email")
        return JSONResponse({"error": "Failed to send email"}, status_code=500)

    return {"ok": True, "link": link, "expires_at": expires_at_iso}


# Twilio SMS/RCS configuration
TWILIO_ACCOUNT_SID = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_PHONE_NUMBER = (os.getenv("TWILIO_PHONE_NUMBER") or "").strip()
TWILIO_RCS_SENDER_ID = (os.getenv("TWILIO_RCS_SENDER_ID") or "").strip()


async def _send_twilio_sms(to_phone: str, body: str, media_url: str | None = None) -> dict:
    """Send SMS via Twilio API"""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_PHONE_NUMBER:
        return {"ok": False, "error": "Twilio not configured"}
    
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    data = {
        "To": to_phone,
        "From": TWILIO_PHONE_NUMBER,
        "Body": body,
    }
    if media_url:
        data["MediaUrl"] = media_url
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                data=data,
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            )
            result = resp.json()
            if resp.status_code in (200, 201):
                return {"ok": True, "sid": result.get("sid")}
            return {"ok": False, "error": result.get("message") or str(result)}
    except Exception as ex:
        logger.error(f"Twilio SMS error: {ex}")
        return {"ok": False, "error": str(ex)}


async def _send_twilio_rcs(to_phone: str, body: str, media_url: str | None = None, button_text: str | None = None, button_url: str | None = None) -> dict:
    """Send RCS Business Message via Twilio Content API"""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return {"ok": False, "error": "Twilio not configured"}
    
    # Use RCS sender ID if available, otherwise fall back to phone number
    from_id = TWILIO_RCS_SENDER_ID or TWILIO_PHONE_NUMBER
    if not from_id:
        return {"ok": False, "error": "No Twilio sender configured"}
    
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    
    # Build content for RCS rich card
    data = {
        "To": to_phone,
        "From": from_id,
        "Body": body,
    }
    
    # Add media if provided
    if media_url:
        data["MediaUrl"] = media_url
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                data=data,
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            )
            result = resp.json()
            if resp.status_code in (200, 201):
                return {"ok": True, "sid": result.get("sid"), "channel": "rcs"}
            # If RCS fails, it may fall back to SMS automatically
            return {"ok": False, "error": result.get("message") or str(result)}
    except Exception as ex:
        logger.error(f"Twilio RCS error: {ex}")
        return {"ok": False, "error": str(ex)}


@router.post("/vaults/share/sms")
async def vaults_share_sms(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Share vault via SMS/RCS text message using Twilio"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    vault = str((payload or {}).get('vault') or '').strip()
    phone = str((payload or {}).get('phone') or '').strip()
    client_name = str((payload or {}).get('client_name') or '').strip()
    use_rcs = bool((payload or {}).get('use_rcs', True))  # Default to RCS
    
    if not vault or not phone:
        return JSONResponse({"error": "vault and phone required"}, status_code=400)
    
    # Normalize phone number (ensure E.164 format)
    phone_clean = ''.join(c for c in phone if c.isdigit() or c == '+')
    if not phone_clean.startswith('+'):
        # Assume US number if no country code
        if len(phone_clean) == 10:
            phone_clean = '+1' + phone_clean
        elif len(phone_clean) == 11 and phone_clean.startswith('1'):
            phone_clean = '+' + phone_clean
        else:
            phone_clean = '+' + phone_clean
    
    # Validate vault exists
    try:
        keys = _read_vault(uid, vault)
        safe_vault = _vault_key(uid, vault)[1]
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)
    
    # Create share token
    expires_in_days = (payload or {}).get('expires_in_days')
    now = datetime.utcnow()
    days = int(expires_in_days or 7)
    exp = now + timedelta(days=days)
    
    token = secrets.token_urlsafe(24)
    rec = {
        "token": token,
        "uid": uid,
        "vault": safe_vault,
        "phone": phone_clean,
        "expires_at": exp.isoformat(),
        "used": False,
        "created_at": now.isoformat(),
        "max_uses": 1,
        "client_name": client_name,
        "shared_via": "sms",
    }
    
    # Download permission
    try:
        perm_raw = str((payload or {}).get('download_permission') or '').strip().lower()
        if perm_raw not in ('proofing', 'proofing_download'):
            perm_raw = 'proofing'
        rec['download_permission'] = perm_raw
    except Exception:
        rec['download_permission'] = 'proofing'
    
    # Client role for proofing permissions
    try:
        role_raw = str((payload or {}).get('client_role') or '').strip().lower()
        if role_raw not in ('viewer', 'editor', 'owner'):
            role_raw = 'editor'
        rec['client_role'] = role_raw
    except Exception:
        rec['client_role'] = 'editor'
    
    # Download pricing (free or paid)
    try:
        download_type = str((payload or {}).get('download_type') or 'free').strip().lower()
        if download_type not in ('free', 'paid'):
            download_type = 'free'
        rec['download_type'] = download_type
        
        download_price_cents = int((payload or {}).get('download_price_cents') or 0)
        rec['download_price_cents'] = max(0, download_price_cents)
        
        # Set licensed flag: true for free downloads, false for paid (until payment completes)
        if download_type == 'free':
            rec['licensed'] = True
        else:
            rec['licensed'] = False
        
        rec['payment_id'] = None
        rec['payment_completed_at'] = None
    except Exception:
        rec['download_type'] = 'free'
        rec['download_price_cents'] = 0
        rec['licensed'] = True
        rec['payment_id'] = None
        rec['payment_completed_at'] = None
    
    # Download limit (1-50, 0 = unlimited)
    try:
        download_limit = int((payload or {}).get('download_limit') or 0)
        if download_limit < 0:
            download_limit = 0
        elif download_limit > 50:
            download_limit = 50
        rec['download_limit'] = download_limit
        rec['download_count'] = 0
    except Exception:
        rec['download_limit'] = 0
        rec['download_count'] = 0
    
    _write_json_key(_share_key(token), rec)
    
    try:
        _pg_upsert_vault_meta(db, uid, safe_vault, {}, visibility="shared")
    except Exception:
        pass
    
    # Build share link - check for custom domain first
    front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
    custom_domain_link = None
    try:
        from models.vault_domain import VaultDomain
        vault_domain = db.query(VaultDomain).filter(
            VaultDomain.uid == uid,
            VaultDomain.vault_name == safe_vault,
            VaultDomain.enabled == True
        ).first()
        if vault_domain:
            custom_domain_link = f"https://{vault_domain.hostname}"
            vault_domain.share_token = token
            db.commit()
    except Exception:
        pass
    link = custom_domain_link if custom_domain_link else f"{front}/#share?token={token}"
    
    # Get studio name from user profile
    studio_name = None
    try:
        fs_db = get_fs_client()
        if fs_db:
            doc = fs_db.collection('users').document(uid).get()
            data = doc.to_dict() if getattr(doc, 'exists', False) else {}
            studio_name = (
                data.get('studioName')
                or data.get('studio_name')
                or data.get('businessName')
                or data.get('business_name')
                or data.get('brand_name')
                or data.get('brandName')
                or data.get('displayName')
                or data.get('display_name')
                or data.get('name')
            )
    except Exception:
        studio_name = None
    if not studio_name:
        try:
            owner_email = (get_user_email_from_uid(uid) or '').strip()
            studio_name = (owner_email.split('@')[0] if '@' in owner_email else owner_email) or "Photomark"
        except Exception:
            studio_name = "Photomark"
    
    # Photo count
    count = len(keys)
    noun = "photo" if count == 1 else "photos"
    
    # Build message
    greeting = f"Hi {client_name}! " if client_name else ""
    message = (
        f"{greeting}{studio_name} has shared {count} {noun} with you for review.\n\n"
        f"View your photos: {link}\n\n"
        f"This link expires in {days} days."
    )
    
    # Get first photo thumbnail for RCS media (optional)
    media_url = None
    if use_rcs and keys:
        try:
            first_key = keys[0]
            media_url = _get_url_for_key(first_key, expires_in=3600)
        except Exception:
            pass
    
    # Send via RCS first, fall back to SMS
    result = None
    if use_rcs and TWILIO_RCS_SENDER_ID:
        result = await _send_twilio_rcs(
            phone_clean, 
            message, 
            media_url=media_url,
            button_text="View Photos",
            button_url=link
        )
    
    # Fall back to SMS if RCS failed or not configured
    if not result or not result.get("ok"):
        result = await _send_twilio_sms(phone_clean, message, media_url=media_url if use_rcs else None)
    
    if not result.get("ok"):
        logger.error(f"Failed to send SMS: {result.get('error')}")
        return JSONResponse({"error": result.get("error") or "Failed to send message"}, status_code=500)
    
    return {
        "ok": True, 
        "link": link, 
        "expires_at": exp.isoformat(),
        "channel": result.get("channel", "sms"),
        "message_sid": result.get("sid")
    }


@router.post("/vaults/publish")
async def vaults_publish(request: Request, payload: dict = Body(...)):
    """Publish a static share page to public storage with a vanity path: /{handle}/vault.
    Returns the public URL. The page embeds the existing share experience (UI hidden) via an iframe.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    vault = str((payload or {}).get('vault') or '').strip()
    custom_handle = str((payload or {}).get('handle') or '').strip()
    expires_in_days = (payload or {}).get('expires_in_days')
    if not vault:
        return JSONResponse({"error": "vault required"}, status_code=400)

    # Validate and normalize vault name
    try:
        _ = _read_vault(uid, vault)
        safe_vault = _vault_key(uid, vault)[1]
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)

    # Create a share token (unlimited uses until expiration)
    now = datetime.utcnow()
    days = int(expires_in_days or 365)
    exp = now + timedelta(days=days)
    token = secrets.token_urlsafe(24)
    rec = {
        "token": token,
        "uid": uid,
        "vault": safe_vault,
        "email": "",
        "expires_at": exp.isoformat(),
        "used": False,
        "created_at": now.isoformat(),
        "max_uses": 0,
    }
    _write_json_key(_share_key(token), rec)

    # Build a handle from provided handle or user email local-part
    def slugify(s: str) -> str:
        s2 = ''.join([c if (c.isalnum() or c in ('-', '_')) else '-' for c in (s or '').strip()]).strip('-_')
        s2 = s2.replace('_','-').lower()
        return s2 or 'user'
    handle = slugify(custom_handle)
    if not handle:
        try:
            email = (get_user_email_from_uid(uid) or '').strip()
            handle = slugify(email.split('@')[0] if '@' in email else email)
        except Exception:
            handle = slugify(uid[:8])
    # Ensure uniqueness by adding short token suffix
    suffix = token[:6].lower()
    handle_final = f"{handle}-{suffix}"

    # Compose public path and URL
    # Path: users/{uid}/published/{handle_final}/vault/index.html
    key = f"users/{uid}/published/{handle_final}/vault/index.html"

    # Frontend origin for iframe source
    front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
    share_url = f"{front}/#share?token={token}&hide_ui=1"

    # Minimal standalone HTML that fills viewport and embeds the share experience
    html = f"""
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{safe_vault} — Vault</title>
  <meta name=\"robots\" content=\"noindex\" />
  <style>
    html,body,iframe{{margin:0;padding:0;height:100%;width:100%;background:#0b0b0b;color:#e5e5e5}}
    .frame{{position:fixed;inset:0;border:0;width:100%;height:100%}}
  </style>
</head>
<body>
  <iframe class=\"frame\" src=\"{share_url}\" allowfullscreen referrerpolicy=\"no-referrer\"></iframe>
</body>
</html>
"""
    try:
        url = upload_bytes(key, html.encode('utf-8'), content_type="text/html; charset=utf-8")
        return {"ok": True, "url": url, "handle": handle_final, "token": token, "expires_at": rec["expires_at"]}
    except Exception as ex:
        logger.warning(f"publish share failed: {ex}")
        return JSONResponse({"error": "publish_failed"}, status_code=500)


@router.post("/vaults/share_link")
async def vaults_share_link(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    vault = str((payload or {}).get('vault') or '').strip()
    if not vault:
        return JSONResponse({"error": "vault required"}, status_code=400)

    # Validate vault exists and get normalized name
    try:
        _ = _read_vault(uid, vault)
        safe_vault = _vault_key(uid, vault)[1]
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)

    expires_at_str = (payload or {}).get('expires_at')
    expires_in_days = (payload or {}).get('expires_in_days')
    now = datetime.utcnow()
    if expires_at_str:
        try:
            exp = datetime.fromisoformat(expires_at_str.replace('Z', '+00:00'))
            if not exp.tzinfo:
                exp = exp.replace(tzinfo=None)
            expires_at_iso = exp.isoformat()
        except Exception:
            return JSONResponse({"error": "invalid expires_at"}, status_code=400)
    else:
        days = int(expires_in_days or 7)
        exp = now + timedelta(days=days)
        expires_at_iso = exp.isoformat()

    token = secrets.token_urlsafe(24)
    rec = {
        "token": token,
        "uid": uid,
        "vault": safe_vault,
        "email": "",
        "expires_at": expires_at_iso,
        "used": False,
        "created_at": now.isoformat(),
        "max_uses": 0,  # unlimited until expiration
    }
    # Optional granular permission for public share link
    try:
        perm_raw = str((payload or {}).get('download_permission') or '').strip().lower()
        if perm_raw not in ('none', 'low', 'high'):
            perm_raw = 'none'
        rec['download_permission'] = perm_raw
    except Exception:
        rec['download_permission'] = 'none'
    # Optional client role for public share link (defaults to viewer)
    try:
        role_raw = str((payload or {}).get('client_role') or '').strip().lower()
        if role_raw not in ('viewer', 'editor', 'owner'):
            role_raw = 'viewer'
        rec['client_role'] = role_raw
    except Exception:
        rec['client_role'] = 'viewer'
    
    # Download limit (1-50, 0 = unlimited)
    try:
        download_limit = int((payload or {}).get('download_limit') or 0)
        if download_limit < 0:
            download_limit = 0
        elif download_limit > 50:
            download_limit = 50
        rec['download_limit'] = download_limit
        rec['download_count'] = 0
    except Exception:
        rec['download_limit'] = 0
        rec['download_count'] = 0
    
    _write_json_key(_share_key(token), rec)

    front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
    
    # Check for custom domain
    custom_domain_link = None
    try:
        from models.vault_domain import VaultDomain
        vault_domain = db.query(VaultDomain).filter(
            VaultDomain.uid == uid,
            VaultDomain.vault_name == safe_vault,
            VaultDomain.enabled == True
        ).first()
        if vault_domain:
            custom_domain_link = f"https://{vault_domain.hostname}"
            vault_domain.share_token = token
            db.commit()
    except Exception:
        pass
    
    link = custom_domain_link if custom_domain_link else f"{front}/#share?token={token}"
    return {"ok": True, "link": link, "token": token, "expires_at": expires_at_iso, "custom_domain": custom_domain_link}


@router.post("/vaults/reel")
async def vaults_create_reel(request: Request, payload: dict = Body(...), background_tasks: BackgroundTasks = None):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    vault = str((payload or {}).get('vault') or '').strip()
    if not vault:
        return JSONResponse({"error": "vault required"}, status_code=400)
    try:
        # Validate vault exists
        _ = _read_vault(uid, vault)
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)

    # Options
    audio_url = str((payload or {}).get('audio_url') or '').strip()
    bpm = None
    try:
        if (payload or {}).get('bpm') is not None:
            bpm = float(payload.get('bpm'))
            if not (0 < bpm < 400):
                bpm = None
    except Exception:
        bpm = None
    beat_marks = []
    try:
        raw = payload.get('beat_marks') or []
        if isinstance(raw, list):
            beat_marks = [float(x) for x in raw if x is not None]
    except Exception:
        beat_marks = []
    transition = str((payload or {}).get('transition') or 'crossfade').strip().lower()
    if transition not in ("crossfade", "slide", "zoom"):
        transition = "crossfade"
    fps = int((payload or {}).get('fps') or 30)
    width = int((payload or {}).get('width') or 1080)
    height = int((payload or {}).get('height') or 1920)
    limit = int((payload or {}).get('limit') or 120)

    # Build image URLs (watermarked)
    try:
        keys = _read_vault(uid, vault)
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)

    img_urls: list[str] = []
    try:
        if s3 and R2_BUCKET:
            for k in keys:
                if k.lower().endswith('.json'):
                    continue
                try:
                    url = _get_url_for_key(k, expires_in=60 * 60)
                    img_urls.append(url)
                except Exception:
                    continue
        else:
            for k in keys:
                if k.lower().endswith('.json'):
                    continue
                img_urls.append(f"/static/{k}")
    except Exception:
        img_urls = []

    if not img_urls:
        return JSONResponse({"error": "no photos in vault"}, status_code=400)

    # Order and limit for sensible default reel length
    try:
        img_urls = img_urls[: max(1, min(limit, len(img_urls)))]
    except Exception:
        img_urls = img_urls[:120]

    # Create job descriptor
    job_id = secrets.token_urlsafe(8)
    created_at = datetime.utcnow().isoformat()
    params = {
        "audio_url": audio_url,
        "bpm": bpm,
        "beat_marks": beat_marks,
        "transition": transition,
        "fps": fps,
        "width": width,
        "height": height,
    }
    job = {
        "id": job_id,
        "uid": uid,
        "vault": vault,
        "created_at": created_at,
        "status": "queued",
        "params": params,
        "images": img_urls,
    }

    # Persist status for polling
    status_key = f"users/{uid}/reels/jobs/{job_id}.status.json"
    try:
        _write_json_key(status_key, job)
    except Exception:
        pass

    # Background render task
    def _bg_render():
        try:
            # Write job JSON to a temp file for the Node renderer
            tmpdir = Path(tempfile.gettempdir()) / "photomark-reels"
            tmpdir.mkdir(parents=True, exist_ok=True)
            job_path = tmpdir / f"{job_id}.json"
            with open(job_path, 'w', encoding='utf-8') as f:
                import json as _json
                _json.dump(job, f)

            out_path = tmpdir / f"{job_id}.mp4"

            # Resolve render script path
            script = os.getenv("REMOTION_RENDER_SCRIPT", str(Path(__file__).resolve().parents[2] / 'reels' / 'render.mjs'))
            # Execute Node renderer
            try:
                subprocess.run(["node", script, "--job", str(job_path), "--out", str(out_path)], check=True)
            except Exception as ex:
                # Update status to failed
                try:
                    fail = job.copy()
                    fail.update({"status": "failed", "error": str(ex)})
                    _write_json_key(status_key, fail)
                except Exception:
                    pass
                return

            # Read file and upload to storage
            try:
                data = out_path.read_bytes()
            except Exception as ex:
                try:
                    fail = job.copy()
                    fail.update({"status": "failed", "error": f"output missing: {ex}"})
                    _write_json_key(status_key, fail)
                except Exception:
                    pass
                return

            # Persist video
            try:
                vid_key = f"users/{uid}/reels/{job_id}.mp4"
                url = upload_bytes(vid_key, data, content_type="video/mp4")
                if not url:
                    if s3 and R2_BUCKET:
                        url = _get_url_for_key(vid_key, expires_in=60 * 60 * 24 * 7)
                    else:
                        url = f"/static/{vid_key}"
                done = job.copy()
                done.update({"status": "done", "video_key": vid_key, "url": url, "completed_at": datetime.utcnow().isoformat()})
                _write_json_key(status_key, done)
            except Exception as ex:
                try:
                    fail = job.copy()
                    fail.update({"status": "failed", "error": str(ex)})
                    _write_json_key(status_key, fail)
                except Exception:
                    pass
        except Exception:
            try:
                fail = job.copy()
                fail.update({"status": "failed", "error": "unexpected renderer error"})
                _write_json_key(status_key, fail)
            except Exception:
                pass

    try:
        if background_tasks is not None:
            background_tasks.add_task(_bg_render)
        else:
            # Fallback synchronous (slower API request), not recommended
            _bg_render()
    except Exception:
        pass

    return {"ok": True, "id": job_id}


@router.get("/vaults/reel/status")
async def vaults_reel_status(request: Request, id: str):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    jid = str((id or '').strip())
    if not jid:
        return JSONResponse({"error": "id required"}, status_code=400)
    key = f"users/{uid}/reels/jobs/{jid}.status.json"
    rec = _read_json_key(key) or {}
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)
    return rec


@router.post("/vaults/share/logo")
async def vaults_share_logo(request: Request, vault: str = Body(..., embed=True), file: UploadFile = File(...)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not vault or not file:
        return JSONResponse({"error": "vault and file required"}, status_code=400)
    
    # Rate limiting
    from utils.rate_limit import check_upload_rate_limit, validate_file_size
    allowed, rate_err = check_upload_rate_limit(uid, file_count=1)
    if not allowed:
        return JSONResponse({"error": rate_err}, status_code=429)
    
    try:
        safe_vault = _vault_key(uid, vault)[1]
        name = file.filename or "logo"
        ext = os.path.splitext(name)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
            ext = ".png"
        ct = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
        }.get(ext, "application/octet-stream")
        data = await file.read()
        
        # Validate file size (logos should be small, max 5MB)
        if len(data) > 5 * 1024 * 1024:
            return JSONResponse({"error": "Logo file too large. Maximum size is 5MB."}, status_code=400)
        date_prefix = datetime.utcnow().strftime('%Y/%m/%d')
        key = f"users/{uid}/vaults/_meta/{safe_vault}/branding/{date_prefix}/logo{ext}"
        url = upload_bytes(key, data, content_type=ct)
        meta = _read_vault_meta(uid, safe_vault) or {}
        meta["share_logo_url"] = url
        _write_vault_meta(uid, safe_vault, meta)
        return {"ok": True, "logo_url": url}
    except Exception as ex:
        logger.warning(f"share logo upload failed: {ex}")
        # Provide additional diagnostics to the client
        hint = ""
        try:
            from core.config import R2_PUBLIC_BASE_URL, R2_BUCKET
            hint = f"Check R2 env and bucket. R2_PUBLIC_BASE_URL={R2_PUBLIC_BASE_URL} bucket={R2_BUCKET}"
        except Exception:
            pass
        return JSONResponse({"error": "upload failed", "hint": hint}, status_code=500)

@router.post("/vaults/welcome")
async def update_vault_welcome_message(request: Request, vault: str = Body(..., embed=True), welcome_message: str = Body(..., embed=True), db: Session = Depends(get_db)):
    """Update vault welcome message displayed to clients"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not vault:
        return JSONResponse({"error": "vault required"}, status_code=400)
    try:
        safe_vault = _vault_key(uid, vault)[1]
        meta = _read_vault_meta(uid, safe_vault) or {}
        msg = str(welcome_message).strip()
        meta["welcome_message"] = msg
        _write_vault_meta(uid, safe_vault, meta)
        _pg_upsert_vault_meta(db, uid, safe_vault, {"welcome_message": msg})
        return {"ok": True, "welcome_message": msg}
    except Exception as ex:
        logger.warning(f"welcome message update failed: {ex}")
        return JSONResponse({"error": "update failed"}, status_code=500)


@router.post("/vaults/display-name")
async def update_vault_display_name(request: Request, vault: str = Body(..., embed=True), display_name: str = Body(..., embed=True), db: Session = Depends(get_db)):
    """Update vault display name"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not vault:
        return JSONResponse({"error": "vault required"}, status_code=400)
    
    name = str(display_name).strip()
    if not name:
        return JSONResponse({"error": "display_name required"}, status_code=400)
    if len(name) > 100:
        return JSONResponse({"error": "display_name too long (max 100 characters)"}, status_code=400)
    
    try:
        safe_vault = _vault_key(uid, vault)[1]
        meta = _read_vault_meta(uid, safe_vault) or {}
        meta["display_name"] = name
        _write_vault_meta(uid, safe_vault, meta)
        _pg_upsert_vault_meta(db, uid, safe_vault, {"display_name": name})
        return {"ok": True, "display_name": name}
    except Exception as ex:
        logger.warning(f"display name update failed: {ex}")
        return JSONResponse({"error": "update failed"}, status_code=500)


@router.get("/vaults/shared/photos")
async def vaults_shared_photos(token: str, password: Optional[str] = None, db: Session = Depends(get_db)):
    if not token or len(token) < 10:
        return JSONResponse({"error": "invalid token"}, status_code=400)

    rec = _read_json_key(_share_key(token))
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)

    try:
        exp = datetime.fromisoformat(str(rec.get('expires_at', '')))
    except Exception:
        exp = None
    now = datetime.utcnow()
    if exp and now > exp:
        return JSONResponse({"error": "expired"}, status_code=410)

    # Allow multiple uses until expiration; ignore any previous 'used' state

    uid = rec.get('uid') or ''
    vault = rec.get('vault') or ''
    email = (rec.get('email') or '').lower()
    if not uid or not vault:
        return JSONResponse({"error": "invalid share"}, status_code=400)

    # Check if vault is protected - clients need password to access
    meta = _read_vault_meta(uid, vault)
    if meta.get('protected'):
        if not _check_password(password or '', meta, uid, vault):
            return JSONResponse({"error": "Vault is protected. Invalid or missing password."}, status_code=403)

    try:
        keys = _read_vault(uid, vault)
        items = [_make_item_from_key(uid, k) for k in keys]
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)

    # If licensed, or password matches the removal password, attach original_url where available
    licensed = bool(rec.get("licensed"))
    removal_unlocked = False
    try:
        if rec.get("remove_pw_hash"):
            import hashlib
            salt = f"share::{token}"
            if hashlib.sha256(((password or '') + salt).encode('utf-8')).hexdigest() == rec.get("remove_pw_hash"):
                removal_unlocked = True
    except Exception:
        removal_unlocked = False
    if licensed or removal_unlocked:
        try:
            if s3 and R2_BUCKET:
                # Build lookup of originals to attach to items
                orig_prefix = f"users/{uid}/originals/"
                original_lookup: dict[str, str] = {}
                try:
                    bucket = s3.Bucket(R2_BUCKET)
                    for o in bucket.objects.filter(Prefix=orig_prefix):
                        ok = o.key
                        if ok.endswith("/"):
                            continue
                        if R2_CUSTOM_DOMAIN and s3_presign_client:
                            o_url = s3_presign_client.generate_presigned_url(
                                "get_object", Params={"Bucket": R2_BUCKET, "Key": ok}, ExpiresIn=60 * 60
                            )
                        else:
                            o_url = s3.meta.client.generate_presigned_url(
                                "get_object", Params={"Bucket": R2_BUCKET, "Key": ok}, ExpiresIn=60 * 60
                            )
                        original_lookup[ok] = o_url
                except Exception:
                    original_lookup = {}

                for it in items:
                    key = it.get("key") or ""
                    try:
                        name = os.path.basename(key)
                        original_key = None
                        if "-o" in name:
                            base_part = name.rsplit("-o", 1)[0]
                            for suf in ("-logo", "-txt"):
                                if base_part.endswith(suf):
                                    base_part = base_part[: -len(suf)]
                                    break
                            dir_part = os.path.dirname(key)  # users/uid/watermarked/YYYY/MM/DD
                            date_part = "/".join(dir_part.split("/")[-3:])
                            for ext in ("jpg","jpeg","png","webp","heic","tif","tiff","bin"):
                                cand = f"users/{uid}/originals/{date_part}/{base_part}-orig.{ext}" if ext != 'bin' else f"users/{uid}/originals/{date_part}/{base_part}-orig.bin"
                                if cand in original_lookup:
                                    original_key = cand
                                    break
                        if original_key and original_key in original_lookup:
                            it["original_key"] = original_key
                            it["original_url"] = original_lookup[original_key]
                            it["url"] = it["original_url"]
                    except Exception:
                        continue
            else:
                # Local filesystem
                original_lookup: set[str] = set()
                orig_dir = os.path.join(STATIC_DIR, f"users/{uid}/originals/")
                if os.path.isdir(orig_dir):
                    for root, _, files in os.walk(orig_dir):
                        for f in files:
                            rel = os.path.relpath(os.path.join(root, f), STATIC_DIR).replace("\\", "/")
                            original_lookup.add(rel)
                for it in items:
                    key = it.get("key") or ""
                    try:
                        dir_part = os.path.dirname(key)  # users/uid/watermarked/YYYY/MM/DD
                        date_part = "/".join(dir_part.split("/")[-3:])
                        name = os.path.basename(key)
                        base_part = name.rsplit("-o", 1)[0] if "-o" in name else os.path.splitext(name)[0]
                        for suf in ("-logo", "-txt"):
                            if base_part.endswith(suf):
                                base_part = base_part[: -len(suf)]
                                break
                        for ext in ("jpg","jpeg","png","webp","heic","tif","tiff","bin"):
                            cand = f"users/{uid}/originals/{date_part}/{base_part}-orig.{ext}" if ext != 'bin' else f"users/{uid}/originals/{date_part}/{base_part}-orig.bin"
                            if cand in original_lookup:
                                it["original_key"] = cand
                                it["original_url"] = f"/static/{cand}"
                                it["url"] = it["original_url"]
                                break
                    except Exception:
                        continue
        except Exception:
            pass

    # Load approvals map to let client show statuses (flatten to by_photo for frontend)
    approvals_raw = _read_json_key(_approval_key(uid, vault)) or {}
    approvals = approvals_raw.get("by_photo") if isinstance(approvals_raw, dict) else {}

    # Load license price (from vault meta)
    try:
        meta = _read_vault_meta(uid, vault) or {}
        price_cents = int(meta.get("license_price_cents") or 0)
        currency = str(meta.get("license_currency") or "USD")
    except Exception:
        price_cents = 0
        currency = "USD"

    # Load favorites map
    favorites = _read_json_key(_favorites_key(uid, vault)) or {}

    # Share customization and descriptions
    share = {}
    try:
        mmeta = _pg_read_vault_meta(db, uid, vault) or _read_vault_meta(uid, vault) or {}
        share = {
            "hide_ui": bool(mmeta.get("share_hide_ui")),
            "color": str(mmeta.get("share_color") or ""),
            "layout": str(mmeta.get("share_layout") or "grid"),
            "logo_url": str(mmeta.get("share_logo_url") or ""),
            "welcome_message": str(mmeta.get("welcome_message") or ""),
            "bg_color": str(mmeta.get("share_bg_color") or ""),
        }
        dmap = mmeta.get("descriptions") or {}
        if isinstance(dmap, dict):
            for it in items:
                try:
                    k = it.get("key") or ""
                    desc = dmap.get(k)
                    if isinstance(desc, str) and desc.strip():
                        it["desc"] = desc
                except Exception:
                    continue
    except Exception:
        pass

    # Build retouch map filtered by token
    retouch = {}
    try:
        q = _read_retouch_queue(uid)
        per_photo: dict[str, dict] = {}
        for it in q:
            try:
                if (it.get("token") or "") != token:
                    continue
                if (it.get("vault") or "") != vault:
                    continue
                k = it.get("key") or ""
                if not k:
                    continue
                st = str(it.get("status") or "open").lower()
                prev = per_photo.get(k)
                if (not prev) or (str(it.get("updated_at") or "") > str(prev.get("updated_at") or "")):
                    per_photo[k] = {
                        "status": st,
                        "id": it.get("id"),
                        "updated_at": it.get("updated_at"),
                        "note": it.get("note") or it.get("comment") or "",
                    }
            except Exception:
                continue
        retouch = {"by_photo": per_photo}
    except Exception:
        retouch = {}

    # Cache-bust image URLs for clients when a retouch update exists (avoid stale CDN/browser cache)
    try:
        vmap = retouch.get("by_photo", {}) if isinstance(retouch, dict) else {}
        if isinstance(vmap, dict) and vmap:
            import re
            for it in items:
                try:
                    k = it.get("key") or ""
                    r = vmap.get(k) or {}
                    ts = str(r.get("updated_at") or "").strip()
                    if not ts:
                        continue
                    v = re.sub(r"[^0-9]", "", ts)[:14] or str(int(datetime.utcnow().timestamp()))
                    def _bust(u: str) -> str:
                        if not isinstance(u, str) or not u:
                            return u
                        sep = '&' if '?' in u else '?'
                        return f"{u}{sep}v={v}"
                    if it.get("url"):
                        it["url"] = _bust(it["url"])
                    if it.get("original_url"):
                        it["original_url"] = _bust(it["original_url"])
                except Exception:
                    continue
    except Exception:
        pass

    # Include granular permission in response
    try:
        share['permission'] = str((rec or {}).get('download_permission') or 'none')
    except Exception:
        share['permission'] = 'none'
    # Include client role in response
    try:
        role = str((rec or {}).get('client_role') or 'viewer')
    except Exception:
        role = 'viewer'
    share['client_role'] = role
    
    # Include brand kit if available (from database)
    brand_kit = {}
    try:
        user = db.query(User).filter(User.uid == uid).first()
        if user:
            brand_kit_data = {
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
            brand_kit = {k: v for k, v in brand_kit_data.items() if v is not None}
    except Exception:
        pass
    
    # Determine final licensed status and price based on download type
    download_type = str((rec or {}).get('download_type') or 'free')
    download_price_cents = int((rec or {}).get('download_price_cents') or 0)
    
    # For paid downloads, check if payment is complete
    if download_type == 'paid' and download_price_cents > 0:
        licensed = bool((rec or {}).get('licensed'))
        final_price_cents = download_price_cents
    elif download_type == 'free' or download_price_cents == 0:
        licensed = True
        final_price_cents = 0
    else:
        # Legacy shares - use existing logic
        licensed = bool((rec or {}).get('licensed'))
        # Keep the vault meta price for legacy shares
        final_price_cents = price_cents
    
    # Include download limit info
    download_limit = int((rec or {}).get('download_limit') or 0)
    download_count = int((rec or {}).get('download_count') or 0)
    
    # Include proofing notification status
    proofing_notified = bool((rec or {}).get('proofing_notified', False))
    
    # Include final delivery status from vault metadata
    final_delivery = None
    try:
        vault_meta = _read_vault_meta(uid, vault) or {}
        if vault_meta.get("final_delivery"):
            final_delivery = vault_meta["final_delivery"]
    except Exception:
        pass
    
    response_data = {
        "photos": items, 
        "vault": vault, 
        "email": email, 
        "approvals": approvals, 
        "favorites": favorites, 
        "licensed": licensed, 
        "removal_unlocked": removal_unlocked, 
        "requires_remove_password": bool((rec or {}).get("remove_pw_hash")), 
        "price_cents": final_price_cents, 
        "currency": currency, 
        "share": share, 
        "retouch": retouch, 
        "download_permission": share['permission'], 
        "client_role": role, 
        "brand_kit": brand_kit, 
        "download_limit": download_limit, 
        "download_count": download_count, 
        "proofing_notified": proofing_notified,
        "owner_uid": uid
    }
    
    # Add final delivery data if available
    if final_delivery:
        response_data["final_delivery"] = final_delivery
    
    return response_data


def _update_approvals(uid: str, vault: str, photo_key: str, client_email: str, action: str, comment: str | None = None, client_name: str | None = None) -> dict:
    """Update approvals file for a vault and return the full approvals map."""
    # Normalize
    action_norm = "approved" if action.lower().startswith("approv") else ("denied" if action.lower().startswith("deny") else None)
    if not action_norm:
        raise ValueError("invalid action")
    client_email = (client_email or "").lower()
    data = _read_json_key(_approval_key(uid, vault)) or {}
    by_photo = data.get("by_photo") or {}
    photo = by_photo.get(photo_key) or {}
    by_email = photo.get("by_email") or {}
    approval_data = {
        "status": action_norm,
        "comment": (comment or ""),
        "at": datetime.utcnow().isoformat(),
    }
    if client_name:
        approval_data["client_name"] = client_name
    by_email[client_email] = approval_data
    photo["by_email"] = by_email
    by_photo[photo_key] = photo
    data["by_photo"] = by_photo
    _write_json_key(_approval_key(uid, vault), data)
    try:
        _touch_approvals_version(uid, vault)
    except Exception:
        pass
    return data


@router.post("/vaults/shared/approve")
async def vaults_shared_approve(payload: ApprovalPayload):
    token = (payload.token or "").strip()
    photo_key = (payload.key or "").strip()
    action = (payload.action or "").strip().lower()
    comment = (payload.comment or "").strip()
    if not token or not photo_key or not action:
        return JSONResponse({"error": "token, key and action required"}, status_code=400)

    rec = _read_json_key(_share_key(token))
    if not rec:
        return JSONResponse({"error": "invalid token"}, status_code=400)

    # Expiry check
    try:
        exp = datetime.fromisoformat(str(rec.get('expires_at', '')))
    except Exception:
        exp = None
    now = datetime.utcnow()
    if exp and now > exp:
        return JSONResponse({"error": "expired"}, status_code=410)

    uid = rec.get('uid') or ''
    vault = rec.get('vault') or ''
    client_email = (rec.get('email') or '').lower()
    client_name = rec.get('client_name') or ''
    if not uid or not vault:
        return JSONResponse({"error": "invalid share"}, status_code=400)
    # Enforce client role permissions: only editor/owner may approve/deny
    role = str((rec or {}).get('client_role') or 'viewer').lower()
    if role not in ('editor', 'owner'):
        return JSONResponse({"error": "action not permitted for your role"}, status_code=403)

    # Validate photo belongs to this uid and vault
    try:
        keys = _read_vault(uid, vault)
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)
    if photo_key not in keys:
        return JSONResponse({"error": "photo not in vault"}, status_code=400)

    try:
        data = _update_approvals(uid, vault, photo_key, client_email, action, comment, client_name)
    except ValueError as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)
    except Exception as ex:
        logger.warning(f"update approvals failed: {ex}")
        return JSONResponse({"error": "failed to save"}, status_code=500)

    # Return current status for this photo
    by_email = (data.get("by_photo", {}).get(photo_key, {}).get("by_email", {}))
    return {"ok": True, "photo": photo_key, "by_email": by_email}


@router.post("/vaults/shared/retouch")
async def vaults_shared_retouch(payload: RetouchRequestPayload):
    token = (payload.token or "").strip()
    photo_key = (payload.key or "").strip()
    comment = (payload.comment or "").strip()
    logger.info(f"Retouch request received - marked_photo_url: {payload.marked_photo_url}, markups: {bool(payload.markups)}")
    if not token or not photo_key:
        return JSONResponse({"error": "token and key required"}, status_code=400)

    rec = _read_json_key(_share_key(token))
    if not rec:
        return JSONResponse({"error": "invalid token"}, status_code=400)

    # Expiry check
    try:
        exp = datetime.fromisoformat(str(rec.get('expires_at', '')))
    except Exception:
        exp = None
    now = datetime.utcnow()
    if exp and now > exp:
        return JSONResponse({"error": "expired"}, status_code=410)

    uid = rec.get('uid') or ''
    vault = rec.get('vault') or ''
    client_email = (rec.get('email') or '').lower()
    client_name = rec.get('client_name') or ''
    if not uid or not vault:
        return JSONResponse({"error": "invalid share"}, status_code=400)
    # Enforce client role permissions: only editor/owner may request retouch
    role = str((rec or {}).get('client_role') or 'viewer').lower()
    if role not in ('editor', 'owner'):
        return JSONResponse({"error": "action not permitted for your role"}, status_code=403)

    # Validate photo belongs to this uid and vault
    try:
        keys = _read_vault(uid, vault)
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)
    if photo_key not in keys:
        return JSONResponse({"error": "photo not in vault"}, status_code=400)

    # Append to queue
    try:
        q = _read_retouch_queue(uid)
        rid = secrets.token_urlsafe(8)
        # Parse annotations either from explicit payload or embedded [annotations] in comment
        ann = None
        try:
            if getattr(payload, "annotations", None):
                ann = payload.annotations
            elif comment:
                marker = "[annotations]"
                idx = comment.lower().find(marker)
                if idx >= 0:
                    raw = (comment[idx + len(marker):] or "").strip()
                    try:
                        ann = json.loads(raw)
                    except Exception:
                        ann = None
        except Exception:
            ann = None
        
        # Parse markups from payload (now in Pydantic model)
        markups = payload.markups if payload.markups else None
        
        # Parse marked_photo_url from payload (now in Pydantic model)
        marked_photo_url = str(payload.marked_photo_url).strip() if payload.marked_photo_url else None
        
        item = {
            "id": rid,
            "uid": uid,
            "vault": vault,
            "token": token,
            "key": photo_key,
            "client_email": client_email,
            "client_name": client_name,
            "comment": comment,
            "status": "open",  # open | in_progress | done
            "requested_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }
        if ann is not None:
            item["annotations"] = ann
        if markups is not None:
            item["markups"] = markups
        if marked_photo_url:
            item["marked_photo_url"] = marked_photo_url
        q.append(item)
        # Keep most recent first (optional)
        try:
            q.sort(key=lambda x: x.get("requested_at", ""), reverse=True)
        except Exception:
            pass
        _write_retouch_queue(uid, q)
        try:
            _touch_retouch_version(uid, vault)
        except Exception:
            pass
    except Exception as ex:
        logger.warning(f"retouch queue append failed: {ex}")
        return JSONResponse({"error": "failed to save"}, status_code=500)

    return {"ok": True, "id": rid}


@router.post("/vaults/shared/favorite")
async def vaults_shared_favorite(payload: FavoritePayload):
    token = (payload.token or "").strip()
    photo_key = (payload.key or "").strip()
    favorite = bool(payload.favorite)
    if not token or not photo_key:
        return JSONResponse({"error": "token and key required"}, status_code=400)

    rec = _read_json_key(_share_key(token))
    if not rec:
        return JSONResponse({"error": "invalid token"}, status_code=400)

    # Expiry check
    try:
        exp = datetime.fromisoformat(str(rec.get('expires_at', '')))
    except Exception:
        exp = None
    now = datetime.utcnow()
    if exp and now > exp:
        return JSONResponse({"error": "expired"}, status_code=410)

    uid = rec.get('uid') or ''
    vault = rec.get('vault') or ''
    client_email = (rec.get('email') or '').lower()
    client_name = rec.get('client_name') or ''
    if not uid or not vault:
        return JSONResponse({"error": "invalid share"}, status_code=400)

    # Validate belongs to vault
    try:
        keys = _read_vault(uid, vault)
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)
    if photo_key not in keys:
        return JSONResponse({"error": "photo not in vault"}, status_code=400)

    # Update favorites structure: { by_photo: { key: { by_email: { email: { favorite: true, at, client_name } } } } }
    data = _read_json_key(_favorites_key(uid, vault)) or {}
    by_photo = data.get("by_photo") or {}
    photo = by_photo.get(photo_key) or {}
    by_email = photo.get("by_email") or {}
    fav_data = {"favorite": favorite, "at": datetime.utcnow().isoformat()}
    if client_name:
        fav_data["client_name"] = client_name
    by_email[client_email] = fav_data
    photo["by_email"] = by_email
    by_photo[photo_key] = photo
    data["by_photo"] = by_photo
    _write_json_key(_favorites_key(uid, vault), data)

    # Maintain sender's Favorites vault for this vault
    try:
        # Choose a machine name and a human display name
        base_name = _vault_key(uid, vault)[1]
        fav_vault_machine = f"favorites__{base_name}"
        fav_display = f"Favorites — {vault}"
        # Add/remove photo in favorites vault
        current = _read_vault(uid, fav_vault_machine)
        if favorite:
            merged = sorted(set(current) | {photo_key})
        else:
            merged = [k for k in current if k != photo_key]
        _write_vault(uid, fav_vault_machine, merged)
        # Ensure meta has a friendly display name and mark as system vault
        meta = _read_vault_meta(uid, fav_vault_machine) or {}
        if meta.get("display_name") != fav_display or meta.get("system_vault") != "favorites":
            meta["display_name"] = fav_display
            meta["system_vault"] = "favorites"
            _write_vault_meta(uid, fav_vault_machine, meta)
    except Exception as ex:
        logger.warning(f"favorites vault update failed: {ex}")

    return {"ok": True, "photo": photo_key, "favorite": favorite}


class ProofingCompletePayload(BaseModel):
    token: str


@router.post("/vaults/shared/notify-proofing-complete")
async def vaults_shared_notify_proofing_complete(payload: ProofingCompletePayload):
    """Client notifies photographer that proofing is complete."""
    token = (payload.token or "").strip()
    if not token:
        return JSONResponse({"error": "token required"}, status_code=400)

    rec = _read_json_key(_share_key(token))
    if not rec:
        return JSONResponse({"error": "invalid token"}, status_code=400)

    # Expiry check
    try:
        exp = datetime.fromisoformat(str(rec.get('expires_at', '')))
    except Exception:
        exp = None
    now = datetime.utcnow()
    if exp and now > exp:
        return JSONResponse({"error": "expired"}, status_code=410)

    uid = rec.get('uid') or ''
    vault = rec.get('vault') or ''
    client_email = (rec.get('email') or '').lower()
    client_name = rec.get('client_name') or ''
    if not uid or not vault:
        return JSONResponse({"error": "invalid share"}, status_code=400)

    # Get approvals data to include summary in email
    try:
        safe_vault = _vault_key(uid, vault)[1]
        approvals_data = _read_json_key(_approval_key(uid, safe_vault)) or {}
        by_photo = approvals_data.get("by_photo") or {}
        
        # Count approvals/denials for this client
        approved_count = 0
        denied_count = 0
        for photo_key, photo_data in by_photo.items():
            by_email = (photo_data or {}).get("by_email") or {}
            client_status = by_email.get(client_email, {})
            status = client_status.get("status", "").lower()
            if status == "approved":
                approved_count += 1
            elif status == "denied":
                denied_count += 1
        
        total_reviewed = approved_count + denied_count
    except Exception:
        approved_count = 0
        denied_count = 0
        total_reviewed = 0

    # Check if already notified to prevent duplicate notifications
    if rec.get('proofing_notified'):
        return JSONResponse({"error": "Photographer has already been notified"}, status_code=400)

    # Send notification email to owner
    try:
        owner_email = (get_user_email_from_uid(uid) or "").strip()
        if owner_email:
            client_display = client_name if client_name else client_email
            subject = f"{client_display} has finished proofing '{vault}'"
            
            summary = f"<strong>{approved_count}</strong> approved"
            if denied_count > 0:
                summary += f", <strong>{denied_count}</strong> need changes"
            
            intro = f"Client <strong>{client_display}</strong> has notified you that they have finished proofing the photos in vault <strong>{vault}</strong>.<br><br>Summary: {summary}"
            
            html = render_email(
                "email_basic.html",
                title="Proofing Complete",
                intro=intro,
                button_label="Review Feedback",
                button_url=(os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/") + "/#gallery",
            )
            text = f"{client_display} has finished proofing photos in vault '{vault}'. {approved_count} approved, {denied_count} need changes."
            send_email_smtp(owner_email, subject, html, text)
            
            # Mark as notified in the share record
            rec['proofing_notified'] = True
            rec['proofing_notified_at'] = datetime.utcnow().isoformat()
            _write_json_key(_share_key(token), rec)
            
            # Mark proofing as complete in vault metadata
            try:
                safe_vault = _vault_key(uid, vault)[1]
                meta = _read_vault_meta(uid, safe_vault)
                meta["proofing_complete"] = {
                    "completed": True,
                    "completed_at": datetime.utcnow().isoformat(),
                    "client_email": client_email,
                    "client_name": client_name,
                    "approved_count": approved_count,
                    "denied_count": denied_count,
                    "total_reviewed": total_reviewed
                }
                _write_vault_meta(uid, safe_vault, meta)
                logger.info(f"Marked proofing complete for vault {safe_vault} by client {client_email}")
            except Exception as ex:
                logger.warning(f"Failed to mark proofing complete in vault metadata: {ex}")
            
    except Exception as ex:
        logger.warning(f"Failed to send proofing complete notification: {ex}")
        return JSONResponse({"error": "Failed to send notification"}, status_code=500)

    return {"ok": True, "message": "Photographer notified"}


@router.get("/vaults/approvals")
async def vaults_approvals(request: Request, vault: str):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        safe_vault = _vault_key(uid, vault)[1]
        data = _read_json_key(_approval_key(uid, safe_vault)) or {}
        return {"vault": safe_vault, "approvals": data}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.get("/vaults/favorites")
async def vaults_favorites(request: Request, vault: str):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        safe_vault = _vault_key(uid, vault)[1]
        data = _read_json_key(_favorites_key(uid, safe_vault)) or {}
        # Transform structure: { by_photo: { key: { by_email: { email: {...} } } } }
        # into: { favorites: { email: [keys] } }
        by_photo = data.get("by_photo") or {}
        result = {}
        for photo_key, photo_data in by_photo.items():
            by_email = (photo_data or {}).get("by_email") or {}
            for email, email_data in by_email.items():
                if isinstance(email_data, dict) and email_data.get("favorite"):
                    if email not in result:
                        result[email] = []
                    result[email].append(photo_key)
        return {"vault": safe_vault, "favorites": result}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.get("/vaults/retouch/queue")
async def retouch_queue(request: Request, email: Optional[str] = None, vault: Optional[str] = None, status: Optional[str] = None):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        items = _read_retouch_queue(uid)
        # Apply optional filters for better UX
        try:
            if email:
                q = str(email or '').strip().lower()
                if q:
                    items = [it for it in items if q in str((it.get('client_email') or '')).lower()]
        except Exception:
            pass
        try:
            if vault:
                raw_v = str(vault or '').strip()
                safe_v = raw_v
                try:
                    # Normalize to machine vault name if photographer typed display name
                    safe_v = _vault_key(uid, raw_v)[1]
                except Exception:
                    safe_v = raw_v
                items = [it for it in items if str(it.get('vault') or '') == safe_v]
        except Exception:
            pass
        try:
            if status:
                s = str(status or '').strip().lower()
                if s:
                    items = [it for it in items if str(it.get('status') or '').lower() == s]
        except Exception:
            pass
        # Optionally, cap to a reasonable size
        return {"queue": items}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/vaults/realtime/version")
async def vaults_realtime_version(request: Request, vault: str):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        safe_vault = _vault_key(uid, vault)[1]
        a_ver = _read_version(_approvals_version_key(uid, safe_vault))
        r_ver = _read_version(_retouch_version_key(uid, safe_vault))
        return {
            "vault": safe_vault,
            "approvals_updated_at": a_ver,
            "retouch_updated_at": r_ver,
            "server_time": datetime.utcnow().isoformat(),
            "suggested_poll_seconds": 5,
        }
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.get("/vaults/realtime/stream")
async def vaults_realtime_stream(request: Request, vault: str, poll_seconds: float = 2.0):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        safe_vault = _vault_key(uid, vault)[1]
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)

    async def event_gen():
        last_a = _read_version(_approvals_version_key(uid, safe_vault))
        last_r = _read_version(_retouch_version_key(uid, safe_vault))
        import json as _json
        # Send initial state
        init = _json.dumps({
            "vault": safe_vault,
            "approvals_updated_at": last_a,
            "retouch_updated_at": last_r,
            "server_time": datetime.utcnow().isoformat(),
        })
        yield f"data: {init}\n\n"
        # Loop until client disconnects
        while True:
            try:
                if await request.is_disconnected():
                    break
            except Exception:
                pass
            try:
                await asyncio.sleep(max(0.5, float(poll_seconds)))
                cur_a = _read_version(_approvals_version_key(uid, safe_vault))
                cur_r = _read_version(_retouch_version_key(uid, safe_vault))
                if cur_a != last_a or cur_r != last_r:
                    last_a, last_r = cur_a, cur_r
                    payload = _json.dumps({
                        "vault": safe_vault,
                        "approvals_updated_at": last_a,
                        "retouch_updated_at": last_r,
                        "server_time": datetime.utcnow().isoformat(),
                    })
                    yield f"data: {payload}\n\n"
                else:
                    # heartbeat to keep connection alive
                    yield ": keep-alive\n\n"
            except Exception:
                # avoid breaking the stream on transient errors
                continue

    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
    return StreamingResponse(event_gen(), media_type="text/event-stream", headers=headers)


@router.post("/vaults/retouch/update")
async def retouch_update(request: Request, payload: dict = Body(...)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    rid = str((payload or {}).get("id") or "").strip()
    status = str((payload or {}).get("status") or "").strip().lower()
    note = str((payload or {}).get("note") or "").strip()
    if not rid:
        return JSONResponse({"error": "id required"}, status_code=400)
    if status and status not in ("open", "in_progress", "done"):
        return JSONResponse({"error": "invalid status"}, status_code=400)
    try:
        items = _read_retouch_queue(uid)
        found = False
        for it in items:
            if it.get("id") == rid:
                if status:
                    # Prevent marking as done without result image
                    if status == "done" and not it.get("result_photo_url"):
                        return JSONResponse({"error": "Cannot mark as done without uploading final result image"}, status_code=400)
                    it["status"] = status
                if note:
                    it["note"] = note
                it["updated_at"] = datetime.utcnow().isoformat()
                found = True
                break
        if not found:
            return JSONResponse({"error": "not found"}, status_code=404)
        _write_retouch_queue(uid, items)
        try:
            _touch_retouch_version(uid, str(it.get("vault") or ""))
        except Exception:
            pass
        try:
            # Notify client via email about the status change (best-effort)
            client_email = (it.get("client_email") or "").strip()
            if client_email:
                photo_name = os.path.basename(it.get("key") or "")
                vault_name = str(it.get("vault") or "")
                st = str(it.get("status") or "open").lower()
                status_label = "Open" if st == "open" else ("In progress" if st == "in_progress" else "Done")
                subject = f"Retouch request update: {status_label} — {photo_name or 'photo'}"
                intro = (
                    f"Your retouch request for <strong>{photo_name or 'the photo'}</strong> in vault <strong>{vault_name}</strong> "
                    f"is now <strong>{status_label}</strong>."
                )
                if note:
                    intro += f"<br>Note: {note}"
                html = render_email(
                    "email_basic.html",
                    title="Retouch status updated",
                    intro=intro,
                    button_label="Open shared vault",
                    button_url=(os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/") + ("/#share?token=" + str(it.get("token")).strip() if str(it.get("token") or "").strip() else "/#share"),
                )
                text = (
                    f"Status for your retouch request is now {status_label}. Photo: {photo_name}. Vault: {vault_name}." +
                    (f" Note: {note}" if note else "")
                )
                try:
                    send_email_smtp(client_email, subject, html, text)
                except Exception:
                    pass
        except Exception:
            pass
        return {"ok": True}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/vaults/retouch/final")
async def retouch_upload_final(request: Request, id: str = Form(...), file: UploadFile = File(...)):
    """Photographer uploads the final retouched version for a retouch request.
    Overwrites the existing photo at the same key to preserve approvals/favorites and shared links.
    Marks the retouch request as done and notifies the client.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # Rate limiting
    from utils.rate_limit import check_upload_rate_limit, validate_file_size
    allowed, rate_err = check_upload_rate_limit(uid, file_count=1)
    if not allowed:
        return JSONResponse({"error": rate_err}, status_code=429)
    
    rid = (id or '').strip()
    if not rid:
        return JSONResponse({"error": "id required"}, status_code=400)
    try:
        items = _read_retouch_queue(uid)
        found = None
        for it in items:
            if str(it.get("id") or "") == rid:
                found = it
                break
        if not found:
            return JSONResponse({"error": "not found"}, status_code=404)
        key = str(found.get("key") or "").strip()
        vault = str(found.get("vault") or "").strip()
        token = str(found.get("token") or "").strip()
        if not key or not vault:
            return JSONResponse({"error": "bad request"}, status_code=400)
        # Validate membership in vault for safety
        try:
            keys = _read_vault(uid, vault)
            if key not in keys:
                return JSONResponse({"error": "photo not in vault"}, status_code=400)
        except Exception:
            pass
        # Read upload bytes
        data = await file.read()
        if not data:
            return JSONResponse({"error": "empty file"}, status_code=400)
        
        # Validate file size
        file_valid, file_err = validate_file_size(len(data), file.filename or '')
        if not file_valid:
            return JSONResponse({"error": file_err}, status_code=400)
        
        # Infer content-type
        name = file.filename or os.path.basename(key) or "image.jpg"
        ext = os.path.splitext(name)[1].lower()
        ct_map = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".heic": "image/heic",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
        }
        ct = ct_map.get(ext, "application/octet-stream")
        
        # Auto-embed IPTC/EXIF metadata if user has it enabled
        try:
            data = auto_embed_metadata_for_user(data, uid)
        except Exception as meta_ex:
            logger.debug(f"Metadata embed skipped: {meta_ex}")
        
        # Overwrite object in-place so that existing keys/approvals remain intact
        try:
            upload_bytes(key, data, content_type=ct)
        except Exception as ex:
            logger.warning(f"retouch final upload failed for {key}: {ex}")
            return JSONResponse({"error": "upload failed"}, status_code=500)
        # Update stored size for this asset (best-effort)
        try:
            db: Session = next(get_db())
            try:
                rec = db.query(GalleryAsset).filter(GalleryAsset.key == key).first()
                if rec:
                    rec.size_bytes = len(data)
                    db.commit()
                else:
                    # Create if missing (rare)
                    rec2 = GalleryAsset(user_uid=uid, vault=vault, key=key, size_bytes=len(data))
                    db.add(rec2)
                    db.commit()
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        except Exception:
            pass
        # Update queue status to done
        try:
            for it in items:
                if str(it.get("id") or "") == rid:
                    it["status"] = "done"
                    it["updated_at"] = datetime.utcnow().isoformat()
                    it["note"] = (it.get("note") or "")
                    break
            _write_retouch_queue(uid, items)
            _touch_retouch_version(uid, vault)
        except Exception:
            pass
        # Notify client (best-effort)
        try:
            client_email = (found.get("client_email") or "").strip()
            if client_email:
                photo_name = os.path.basename(key)
                subject = f"Retouched photo ready — {photo_name}"
                intro = (
                    f"Your retouch request for <strong>{photo_name}</strong> in vault <strong>{vault}</strong> is now <strong>Done</strong>."
                )
                html = render_email(
                    "email_basic.html",
                    title="Retouched version uploaded",
                    intro=intro,
                    button_label="Open shared vault",
                    button_url=(os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/") + ("/#share?token=" + token if token else "/#share"),
                )
                text = f"Your retouched photo is ready: {photo_name} in vault {vault}."
                try:
                    send_email_smtp(client_email, subject, html, text)
                except Exception:
                    pass
        except Exception:
            pass
        # Respond with basic info
        url = None
        try:
            if s3 and R2_BUCKET:
                url = _get_url_for_key(key, expires_in=60 * 60 * 24 * 7)
            else:
                url = f"/static/{key}"
        except Exception:
            url = None
        return {"ok": True, "key": key, "url": url}
    except Exception as ex:
        logger.warning(f"retouch_upload_final error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/vaults/retouch/upload-result")
async def retouch_upload_result(request: Request, retouch_id: str = Form(...), file: UploadFile = File(...)):
    """Alias endpoint for uploading retouched result. Accepts retouch_id instead of id.
    Photographer uploads the final retouched version for a retouch request.
    Overwrites the existing photo at the same key to preserve approvals/favorites and shared links.
    Marks the retouch request as done and notifies the client.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # Rate limiting
    from utils.rate_limit import check_upload_rate_limit, validate_file_size
    allowed, rate_err = check_upload_rate_limit(uid, file_count=1)
    if not allowed:
        return JSONResponse({"error": rate_err}, status_code=429)
    
    rid = (retouch_id or '').strip()
    if not rid:
        return JSONResponse({"error": "retouch_id required"}, status_code=400)
    try:
        items = _read_retouch_queue(uid)
        found = None
        for it in items:
            if str(it.get("id") or "") == rid:
                found = it
                break
        if not found:
            return JSONResponse({"error": "Retouch request not found"}, status_code=404)
        key = str(found.get("key") or "").strip()
        vault = str(found.get("vault") or "").strip()
        token = str(found.get("token") or "").strip()
        if not key or not vault:
            return JSONResponse({"error": "Invalid retouch request data"}, status_code=400)
        # Validate membership in vault for safety
        try:
            keys = _read_vault(uid, vault)
            if key not in keys:
                return JSONResponse({"error": "Photo not in vault"}, status_code=400)
        except Exception:
            pass
        # Read upload bytes
        data = await file.read()
        if not data:
            return JSONResponse({"error": "Empty file"}, status_code=400)
        # Infer content-type
        name = file.filename or os.path.basename(key) or "image.jpg"
        ext = os.path.splitext(name)[1].lower()
        ct_map = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".heic": "image/heic",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
        }
        ct = ct_map.get(ext, "application/octet-stream")
        
        # Auto-embed IPTC/EXIF metadata if user has it enabled
        try:
            data = auto_embed_metadata_for_user(data, uid)
        except Exception as meta_ex:
            logger.debug(f"Metadata embed skipped: {meta_ex}")
        
        # Check file size (limit to 50MB for retouch uploads)
        max_size = 50 * 1024 * 1024  # 50MB
        if len(data) > max_size:
            logger.warning(f"retouch upload-result file too large: {len(data)} bytes for {key}")
            return JSONResponse({"error": f"File too large. Maximum size is 50MB, got {len(data) / (1024*1024):.1f}MB"}, status_code=400)
        
        # Overwrite object in-place so that existing keys/approvals remain intact
        try:
            upload_bytes(key, data, content_type=ct)
        except Exception as ex:
            logger.error(f"retouch upload-result failed for {key}: {ex}", exc_info=True)
            return JSONResponse({"error": f"Upload failed: {str(ex)}"}, status_code=500)
        # Update stored size for this asset (best-effort)
        try:
            db: Session = next(get_db())
            try:
                rec = db.query(GalleryAsset).filter(GalleryAsset.key == key).first()
                if rec:
                    rec.size_bytes = len(data)
                    db.commit()
                else:
                    rec2 = GalleryAsset(user_uid=uid, vault=vault, key=key, size_bytes=len(data))
                    db.add(rec2)
                    db.commit()
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        except Exception:
            pass
        # Update queue status to done
        try:
            for it in items:
                if str(it.get("id") or "") == rid:
                    it["status"] = "done"
                    it["updated_at"] = datetime.utcnow().isoformat()
                    it["note"] = (it.get("note") or "")
                    break
            _write_retouch_queue(uid, items)
            _touch_retouch_version(uid, vault)
        except Exception:
            pass
        # Notify client (best-effort)
        try:
            client_email = (found.get("client_email") or "").strip()
            if client_email:
                photo_name = os.path.basename(key)
                subject = f"Retouched photo ready — {photo_name}"
                intro = (
                    f"Your retouch request for <strong>{photo_name}</strong> in vault <strong>{vault}</strong> is now <strong>Done</strong>."
                )
                html = render_email(
                    "email_basic.html",
                    title="Retouched version uploaded",
                    intro=intro,
                    button_label="Open shared vault",
                    button_url=(os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/") + ("/#share?token=" + token if token else "/#share"),
                )
                text = f"Your retouched photo is ready: {photo_name} in vault {vault}."
                try:
                    send_email_smtp(client_email, subject, html, text)
                except Exception:
                    pass
        except Exception:
            pass
        # Respond with success
        url = None
        try:
            if s3 and R2_BUCKET:
                url = _get_url_for_key(key, expires_in=60 * 60 * 24 * 7)
            else:
                url = f"/static/{key}"
        except Exception:
            url = None
        return {"ok": True, "key": key, "url": url, "status": "done"}
    except Exception as ex:
        logger.warning(f"retouch_upload_result error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/vaults/shared/checkout")
async def vaults_shared_checkout(payload: CheckoutPayload, request: Request):
    token = (payload.token or "").strip()
    if not token:
        return JSONResponse({"error": "token required"}, status_code=400)

    rec = _read_json_key(_share_key(token))
    if not rec:
        return JSONResponse({"error": "invalid token"}, status_code=400)

    try:
        exp = datetime.fromisoformat(str(rec.get("expires_at", "")))
    except Exception:
        exp = None

    now = datetime.utcnow()
    if exp and now > exp:
        return JSONResponse({"error": "expired"}, status_code=410)

    uid = rec.get("uid") or ""
    vault = rec.get("vault") or ""
    if not uid or not vault:
        return JSONResponse({"error": "invalid share"}, status_code=400)

    # Get price from share record (new flow) or vault meta (legacy)
    download_type = str(rec.get('download_type') or 'free')
    download_price_cents = int(rec.get('download_price_cents') or 0)
    
    # Use share-specific price if available, otherwise fall back to vault meta
    if download_price_cents > 0:
        amount = download_price_cents
        currency = "USD"
    else:
        # Legacy: get price from vault meta
        meta = _read_vault_meta(uid, vault) or {}
        amount = int(meta.get("license_price_cents") or 0)
        currency = str(meta.get("license_currency") or "USD")

    if amount <= 0:
        return JSONResponse({"error": "license not available"}, status_code=400)

    # Build success/cancel URLs to return user to the same share link
    front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
    return_url = f"{front}/#share?token={token}"

    try:
        # Build payload variants using shared Dodo helper
        from utils.dodo import create_checkout_link

        # Ensure webhook can resolve the purchasing user reliably
        # Include both uid aliases in metadata and reference fields at the top level
        base_metadata = {"token": token, "uid": uid, "user_uid": uid, "vault": vault}
        business_id = (os.getenv("DODO_BUSINESS_ID") or "").strip()
        brand_id = (os.getenv("DODO_BRAND_ID") or "").strip()
        common_top = {**({"business_id": business_id} if business_id else {}), **({"brand_id": brand_id} if brand_id else {})}
        ref_fields = {"client_reference_id": uid, "reference_id": uid, "external_id": uid}

        alt_payloads = [
            {
                **common_top,
                **ref_fields,
                "amount": amount,
                "currency": currency,
                "quantity": 1,
                "metadata": base_metadata,
                "return_url": return_url,
            },
            {
                **common_top,
                **ref_fields,
                "amount": amount,
                "currency": currency,
                "payment_link": True,
                "metadata": base_metadata,
                "return_url": return_url,
            },
            {
                **common_top,
                **ref_fields,
                "items": [{"amount": amount, "currency": currency, "quantity": 1}],
                "metadata": base_metadata,
                "return_url": return_url,
            },
            {
                **common_top,
                **ref_fields,
                "payment_details": {"amount": amount, "currency": currency, "quantity": 1},
                "metadata": base_metadata,
                "return_url": return_url,
            },
        ]

        link, details = await create_checkout_link(alt_payloads)
        if link:
            return {"checkout_url": link}
        logger.warning(f"[vaults.checkout] failed to create payment link: {details}")
        return JSONResponse({"error": "link_creation_failed", "details": details}, status_code=502)

    except httpx.HTTPError as he:
        logger.warning(f"Dodo checkout network error: {he}")
        return JSONResponse({"error": "network error"}, status_code=502)
    except Exception as ex:
        logger.warning(f"Dodo checkout error: {ex}")
        return JSONResponse({"error": "checkout failed"}, status_code=502)


@router.post("/api/payments/dodo/webhook")
async def dodo_webhook(request: Request):
    # Verify signature if provided
    try:
        sig = request.headers.get("X-Dodo-Signature", "")
        body = await request.body()
        # Minimal shared-secret check (replace with real HMAC if Dodo requires)
        if DODO_WEBHOOK_SECRET and (DODO_WEBHOOK_SECRET not in sig):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        evt = json.loads(body.decode("utf-8"))
    except Exception:
        return JSONResponse({"error": "bad payload"}, status_code=400)

    event_type = str(evt.get("type") or "").lower()
    data = evt.get("data") or {}
    obj = data.get("object") or data  # tolerate different envelope shapes
    metadata = (obj.get("metadata") if isinstance(obj, dict) else None) or {}
    token = (metadata.get("token") or "").strip()

    # Helper: persist license file using HMAC signature
    def _issue_license(rec: dict):
        try:
            uid = rec.get("uid") or ""
            vault = rec.get("vault") or ""
            email = (rec.get("email") or "").lower()
            if not uid or not vault or not email:
                return False
            issued_at = datetime.utcnow().isoformat()
            payload = {
                "issuer": LICENSE_ISSUER or "Photomark",
                "uid": uid,
                "vault": vault,
                "email": email,
                "token": rec.get("token") or "",
                "issued_at": issued_at,
                "version": 1,
            }
            body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

            signature = None
            algo = None
            # Prefer asymmetric signing if key provided
            if LICENSE_PRIVATE_KEY:
                try:
                    from cryptography.hazmat.primitives import serialization, hashes
                    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
                    from cryptography.exceptions import InvalidSignature

                    # Try Ed25519 first
                    try:
                        priv = Ed25519PrivateKey.from_private_bytes(
                            serialization.load_pem_private_key(LICENSE_PRIVATE_KEY.encode("utf-8"), password=None).private_bytes(
                                encoding=serialization.Encoding.Raw,
                                format=serialization.PrivateFormat.Raw,
                                encryption_algorithm=serialization.NoEncryption(),
                            )
                        )
                        signature = priv.sign(body)
                        algo = "Ed25519"
                    except Exception:
                        # Fallback to RSA PKCS1v15-SHA256
                        from cryptography.hazmat.primitives.asymmetric import rsa, padding
                        key = serialization.load_pem_private_key(LICENSE_PRIVATE_KEY.encode("utf-8"), password=None)
                        signature = key.sign(body, padding.PKCS1v15(), hashes.SHA256())
                        algo = "RSA-PKCS1v15-SHA256"
                except Exception:
                    signature = None
                    algo = None

            if not signature and LICENSE_SECRET:
                import hmac, hashlib
                signature = hmac.new((LICENSE_SECRET or "").encode("utf-8"), body, hashlib.sha256).hexdigest().encode("utf-8")
                algo = "HMAC-SHA256"

            if not signature:
                return False

            import base64
            sig_b64 = base64.b64encode(signature).decode("ascii")
            license_doc = {"license": payload, "signature": sig_b64, "algo": algo}
            key = f"licenses/{uid}/{vault}/{email}.json"
            _write_json_key(key, license_doc)
            return True
        except Exception as ex:
            logger.warning(f"issue_license failed: {ex}")
            return False

    if event_type in ("payment.succeeded", "checkout.session.completed") and token:
        rec = _read_json_key(_share_key(token)) or {}
        if rec:
            rec["licensed"] = True
            # Track payment id if provided
            try:
                pay_id = obj.get("id") or obj.get("payment_id") or obj.get("session_id")
                if pay_id:
                    rec["payment_id"] = str(pay_id)
            except Exception:
                pass
            _write_json_key(_share_key(token), rec)
            _issue_license(rec)

            # Send confirmation email to the client with link to originals
            try:
                front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
                share_link = f"{front}/#share?token={token}"
                api_base = str(request.base_url).rstrip("/")
                download_link = f"{api_base}/api/vaults/shared/originals.zip?token={token}"

                subject = "Your license purchase is confirmed"
                intro = (
                    "Thank you for your purchase. The license is now active and you can download the original, "
                    "unwatermarked photos from your shared vault."
                )
                html = render_email(
                    "email_basic.html",
                    title="License purchase successful",
                    intro=intro,
                    button_label="Open shared vault",
                    button_url=share_link,
                    footer_note=f"If the button doesn't work, use this direct link: <a href=\"{download_link}\">Download originals</a>",
                )
                text = (
                    "Your license purchase is confirmed. You can access originals here: "
                    f"{share_link}\nDirect download: {download_link}"
                )
                to_email = (rec.get("email") or "").strip()
                if to_email:
                    send_email_smtp(to_email, subject, html, text)
            except Exception:
                # Best-effort email; ignore failures
                pass
        return {"ok": True}

    return {"ok": True}


@router.get("/vaults/shared/originals.zip")
async def vaults_shared_originals_zip(request: Request, token: str, password: Optional[str] = None, keys: Optional[str] = None):
    if not token or len(token) < 10:
        return JSONResponse({"error": "invalid token"}, status_code=400)

    rec = _read_json_key(_share_key(token))
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)

    try:
        exp = datetime.fromisoformat(str(rec.get('expires_at', '')))
    except Exception:
        exp = None
    now = datetime.utcnow()
    if exp and now > exp:
        return JSONResponse({"error": "expired"}, status_code=410)

    # Check download authorization based on download type
    download_permission = str(rec.get('download_permission') or '').strip().lower()
    download_type = str(rec.get('download_type') or 'free').strip().lower()
    download_price_cents = int(rec.get('download_price_cents') or 0)
    
    allow_download = False
    
    # Check based on download permission and type
    if download_permission == 'proofing_download':
        # New flow: check if free or paid+licensed
        if download_type == 'free' or download_price_cents == 0:
            allow_download = True
        elif download_type == 'paid' and bool(rec.get('licensed')):
            allow_download = True
    elif download_permission == 'high':
        # Legacy high-res permission
        allow_download = True
    else:
        # Legacy proofing-only or other: check licensed flag
        allow_download = bool(rec.get("licensed"))
    
    # Also allow if correct removal password provided
    if not allow_download:
        try:
            if rec.get("remove_pw_hash"):
                import hashlib
                salt = f"share::{token}"
                if hashlib.sha256(((password or '') + salt).encode('utf-8')).hexdigest() == rec.get("remove_pw_hash"):
                    allow_download = True
        except Exception:
            pass
    
    if not allow_download:
        if download_type == 'paid' and download_price_cents > 0:
            return JSONResponse({"error": "Payment required. Please purchase usage rights to download."}, status_code=402)
        return JSONResponse({"error": "not licensed"}, status_code=403)
    
    # Check download limit
    download_limit = int(rec.get('download_limit') or 0)
    download_count = int(rec.get('download_count') or 0)
    if download_limit > 0 and download_count >= download_limit:
        return JSONResponse({"error": "Download limit reached. This share link has exceeded its maximum number of downloads."}, status_code=403)

    uid = rec.get('uid') or ''
    vault = rec.get('vault') or ''
    if not uid or not vault:
        return JSONResponse({"error": "invalid share"}, status_code=400)

    # Check if vault is protected - clients need password to access
    meta = _read_vault_meta(uid, vault)
    if meta.get('protected'):
        if not _check_password(password or '', meta, uid, vault):
            return JSONResponse({"error": "Vault is protected. Invalid or missing password."}, status_code=403)

    # Collect vault keys and map to original keys
    try:
        vault_keys = _read_vault(uid, vault)
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)

    # Optional selection filter
    try:
        selected: list[str] = []
        if keys:
            raw = [x for x in (keys.split(',') if ',' in keys else [keys]) if x]
            raw_set = set(raw)
            selected = [k for k in vault_keys if k in raw_set]
        else:
            selected = vault_keys
    except Exception:
        selected = vault_keys

    original_items: list[tuple[str, bytes]] = []  # (arcname, content)

    def map_original_key(wm_key: str) -> Optional[str]:
        try:
            dir_part = os.path.dirname(wm_key)
            date_part = "/".join(dir_part.split("/")[-3:])
            name = os.path.basename(wm_key)
            base_part = name.rsplit("-o", 1)[0] if "-o" in name else os.path.splitext(name)[0]
            for suf in ("-logo", "-txt"):
                if base_part.endswith(suf):
                    base_part = base_part[: -len(suf)]
                    break
            for ext in ("jpg","jpeg","png","webp","heic","tif","tiff","bin"):
                cand = f"users/{uid}/originals/{date_part}/{base_part}-orig.{ext}" if ext != 'bin' else f"users/{uid}/originals/{date_part}/{base_part}-orig.bin"
                # We don't know existence fast; try fetch
                if s3 and R2_BUCKET:
                    try:
                        obj = s3.Object(R2_BUCKET, cand)
                        _ = obj.content_length  # triggers head request
                        return cand
                    except Exception:
                        continue
                else:
                    local_path = os.path.join(STATIC_DIR, cand)
                    if os.path.isfile(local_path):
                        return cand
        except Exception:
            return None
        return None

    try:
        for k in selected:
            ok = map_original_key(k)
            if not ok:
                continue
            arcname = os.path.basename(ok)
            try:
                if s3 and R2_BUCKET:
                    obj = s3.Object(R2_BUCKET, ok)
                    content = obj.get()["Body"].read()
                else:
                    with open(os.path.join(STATIC_DIR, ok), "rb") as f:
                        content = f.read()
                original_items.append((arcname, content))
            except Exception:
                continue
    except Exception:
        pass

    if not original_items:
        return JSONResponse({"error": "no originals available"}, status_code=404)

    # Increment download count
    try:
        rec['download_count'] = int(rec.get('download_count') or 0) + 1
        _write_json_key(_share_key(token), rec)
    except Exception:
        pass

    # Calculate total size for analytics
    total_size = sum(len(content) for _, content in original_items)
    
    # Track download analytics
    try:
        await _track_download_analytics(
            request=request,
            owner_uid=uid,
            vault_name=vault,
            share_token=token,
            download_type="original",
            photo_keys=selected,
            file_count=len(original_items),
            total_size_bytes=total_size,
            is_paid=(download_type == 'paid' and download_price_cents > 0),
            payment_amount_cents=download_price_cents if download_type == 'paid' else None,
            payment_id=rec.get('payment_id')
        )
    except Exception as e:
        logger.error(f"Failed to track download analytics: {e}")

    # Build zip in-memory
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in original_items:
            zf.writestr(name, content)
    mem.seek(0)
    headers = {"Content-Disposition": f"attachment; filename=\"{vault}-originals.zip\""}
    return StreamingResponse(mem, media_type="application/zip", headers=headers)


@router.get("/vaults/shared/lowres.zip")
async def vaults_shared_lowres_zip(request: Request, token: str, password: Optional[str] = None, keys: Optional[str] = None, max_size: Optional[int] = 1920, quality: Optional[int] = 60):
    if not token or len(token) < 10:
        return JSONResponse({"error": "invalid token"}, status_code=400)

    rec = _read_json_key(_share_key(token))
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)

    # Expiration check
    try:
        exp = datetime.fromisoformat(str(rec.get('expires_at', '')))
    except Exception:
        exp = None
    now = datetime.utcnow()
    if exp and now > exp:
        return JSONResponse({"error": "expired"}, status_code=410)

    # Permission check: allow low-res for 'low', 'high', or 'proofing_download'
    perm = str((rec.get('download_permission') or '')).strip().lower()
    if perm not in ('low', 'high', 'proofing_download'):
        return JSONResponse({"error": "permission_denied"}, status_code=403)
    
    # Check download limit
    download_limit = int(rec.get('download_limit') or 0)
    download_count = int(rec.get('download_count') or 0)
    if download_limit > 0 and download_count >= download_limit:
        return JSONResponse({"error": "Download limit reached. This share link has exceeded its maximum number of downloads."}, status_code=403)

    uid = rec.get('uid') or ''
    vault = rec.get('vault') or ''
    if not uid or not vault:
        return JSONResponse({"error": "invalid share"}, status_code=400)

    # Protected vaults still require password to access contents
    meta = _read_vault_meta(uid, vault)
    if meta.get('protected'):
        if not _check_password(password or '', meta, uid, vault):
            return JSONResponse({"error": "Vault is protected. Invalid or missing password."}, status_code=403)

    # Collect vault keys
    try:
        vault_keys = _read_vault(uid, vault)
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)

    # Optional limit to selected keys
    try:
        selected: list[str] = []
        if keys:
            raw = [x for x in (keys.split(',') if ',' in keys else [keys]) if x]
            raw_set = set(raw)
            selected = [k for k in vault_keys if k in raw_set]
        else:
            selected = vault_keys
    except Exception:
        selected = vault_keys

    # Prepare low-res conversions
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return JSONResponse({"error": "image_processing_unavailable"}, status_code=503)

    low_items: list[tuple[str, bytes]] = []

    def load_bytes(key: str) -> Optional[bytes]:
        try:
            if s3 and R2_BUCKET:
                obj = s3.Object(R2_BUCKET, key)
                return obj.get()["Body"].read()
            else:
                with open(os.path.join(STATIC_DIR, key), "rb") as f:
                    return f.read()
        except Exception:
            return None

    def map_original_key(wm_key: str) -> Optional[str]:
        try:
            dir_part = os.path.dirname(wm_key)
            date_part = "/".join(dir_part.split("/")[-3:])
            name = os.path.basename(wm_key)
            base_part = name.rsplit("-o", 1)[0] if "-o" in name else os.path.splitext(name)[0]
            for suf in ("-logo", "-txt"):
                if base_part.endswith(suf):
                    base_part = base_part[: -len(suf)]
                    break
            for ext in ("jpg","jpeg","png","webp","heic","tif","tiff","bin"):
                cand = f"users/{uid}/originals/{date_part}/{base_part}-orig.{ext}" if ext != 'bin' else f"users/{uid}/originals/{date_part}/{base_part}-orig.bin"
                if s3 and R2_BUCKET:
                    try:
                        obj = s3.Object(R2_BUCKET, cand)
                        _ = obj.content_length
                        return cand
                    except Exception:
                        continue
                else:
                    local_path = os.path.join(STATIC_DIR, cand)
                    if os.path.isfile(local_path):
                        return cand
        except Exception:
            return None
        return None

    max_edge = int(max_size or 1920)
    quality_val = int(quality or 60)

    for wm in selected:
        orig_key = map_original_key(wm)
        src_key = orig_key or wm
        data = load_bytes(src_key)
        if not data:
            continue
        try:
            import io as _io
            bio = _io.BytesIO(data)
            img = Image.open(bio)
            img = img.convert('RGB')
            w, h = img.size
            scale = 1.0
            try:
                m = float(max_edge)
                scale = min(1.0, m / float(max(w, h)))
            except Exception:
                scale = 1.0
            if scale < 1.0:
                new_size = (int(w * scale), int(h * scale))
                try:
                    img = img.resize(new_size, resample=Image.LANCZOS)
                except Exception:
                    img = img.resize(new_size)
            out = _io.BytesIO()
            img.save(out, format='JPEG', quality=quality_val, optimize=True)
            low_items.append((os.path.splitext(os.path.basename(src_key))[0] + "-lowres.jpg", out.getvalue()))
        except Exception:
            continue

    if not low_items:
        return JSONResponse({"error": "no_images"}, status_code=404)

    # Increment download count
    try:
        rec['download_count'] = int(rec.get('download_count') or 0) + 1
        _write_json_key(_share_key(token), rec)
    except Exception:
        pass

    # Calculate total size for analytics
    total_size = sum(len(content) for _, content in low_items)
    
    # Track download analytics
    try:
        await _track_download_analytics(
            request=request,
            owner_uid=uid,
            vault_name=vault,
            share_token=token,
            download_type="lowres",
            photo_keys=selected,
            file_count=len(low_items),
            total_size_bytes=total_size,
            is_paid=False,  # Lowres downloads are typically free
        )
    except Exception as e:
        logger.error(f"Failed to track lowres download analytics: {e}")

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in low_items:
            zf.writestr(name, content)
    mem.seek(0)
    headers = {"Content-Disposition": f"attachment; filename=\"{vault}-lowres.zip\""}
    return StreamingResponse(mem, media_type="application/zip", headers=headers)


@router.get("/licenses/public-key")
async def licenses_public_key():
    try:
        from fastapi.responses import PlainTextResponse
        pem = (LICENSE_PUBLIC_KEY or "").strip()
        if pem:
            return PlainTextResponse(pem, media_type="text/plain; charset=utf-8")
        if (LICENSE_PRIVATE_KEY or "").strip():
            from cryptography.hazmat.primitives import serialization
            key = serialization.load_pem_private_key(LICENSE_PRIVATE_KEY.encode("utf-8"), password=None)
            pub = key.public_key()
            pub_pem = pub.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("utf-8")
            return PlainTextResponse(pub_pem, media_type="text/plain; charset=utf-8")
        return JSONResponse({"error": "no key configured"}, status_code=404)
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=500)


class LicenseDoc(BaseModel):
    license: dict
    signature: str  # base64
    algo: str


@router.post("/licenses/verify")
async def licenses_verify(doc: LicenseDoc):
    try:
        payload = doc.license or {}
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        import base64
        sig = base64.b64decode((doc.signature or "").encode("ascii"))
        algo = (doc.algo or "").upper()

        if algo == "ED25519":
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            pem = (LICENSE_PUBLIC_KEY or "").strip()
            if not pem and (LICENSE_PRIVATE_KEY or "").strip():
                # derive from private key
                key = serialization.load_pem_private_key(LICENSE_PRIVATE_KEY.encode("utf-8"), password=None)
                pub = key.public_key()
                pem = pub.public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                ).decode("utf-8")
            if not pem:
                return JSONResponse({"ok": False, "error": "no public key configured"}, status_code=503)
            pub = serialization.load_pem_public_key(pem.encode("utf-8"))
            pub.verify(sig, body)
            return {"ok": True}

        if algo.startswith("RSA"):
            from cryptography.hazmat.primitives import serialization, hashes
            from cryptography.hazmat.primitives.asymmetric import padding
            pem = (LICENSE_PUBLIC_KEY or "").strip()
            if not pem and (LICENSE_PRIVATE_KEY or "").strip():
                key = serialization.load_pem_private_key(LICENSE_PRIVATE_KEY.encode("utf-8"), password=None)
                pub = key.public_key()
                pem = pub.public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                ).decode("utf-8")
            if not pem:
                return JSONResponse({"ok": False, "error": "no public key configured"}, status_code=503)
            pub = serialization.load_pem_public_key(pem.encode("utf-8"))
            pub.verify(sig, body, padding.PKCS1v15(), hashes.SHA256())
            return {"ok": True}

        if algo == "HMAC-SHA256":
            import hmac, hashlib
            if not LICENSE_SECRET:
                return JSONResponse({"ok": False, "error": "no HMAC secret configured"}, status_code=503)
            raw = hmac.new(LICENSE_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
            hex_bytes = hmac.new(LICENSE_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest().encode("utf-8")
            if hmac.compare_digest(sig, raw) or hmac.compare_digest(sig, hex_bytes):
                return {"ok": True}
            return JSONResponse({"ok": False, "error": "invalid signature"}, status_code=400)

        return JSONResponse({"ok": False, "error": "unknown algo"}, status_code=400)
    except Exception as ex:
        return JSONResponse({"ok": False, "error": str(ex)}, status_code=400)


@router.get("/vaults/slideshow")
async def get_vault_slideshow(request: Request, vault: str):
    """Get slideshow configuration for a vault"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        safe_vault = _vault_key(uid, vault)[1]
        meta = _read_vault_meta(uid, safe_vault) or {}
        slideshow = meta.get("slideshow", [])
        
        # Validate slideshow items still exist in vault
        vault_keys = set(_read_vault(uid, safe_vault))
        valid_slideshow = []
        
        for item in slideshow:
            if isinstance(item, dict) and item.get("key") in vault_keys:
                valid_slideshow.append({
                    "key": item["key"],
                    "title": item.get("title"),
                    "url": _get_url_for_key(item["key"], expires_in=3600),
                    "name": os.path.basename(item["key"])
                })
        
        return {"slideshow": valid_slideshow}
    except Exception as ex:
        logger.error(f"Failed to get vault slideshow: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.post("/vaults/slideshow")
async def update_vault_slideshow(request: Request, payload: SlideshowUpdatePayload):
    """Update slideshow configuration for a vault"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    v = (payload.vault or '').strip()
    if not v:
        return JSONResponse({"error": "vault required"}, status_code=400)
    
    try:
        safe_vault = _vault_key(uid, v)[1]
        meta = _read_vault_meta(uid, safe_vault) or {}
        
        # Validate that all slideshow keys exist in the vault
        vault_keys = set(_read_vault(uid, safe_vault))
        valid_slideshow = []
        
        for item in payload.slideshow:
            if item.key in vault_keys:
                valid_slideshow.append({
                    "key": item.key,
                    "title": item.title
                })
            else:
                logger.warning(f"Slideshow item key {item.key} not found in vault {safe_vault}")
        
        meta["slideshow"] = valid_slideshow
        _write_vault_meta(uid, safe_vault, meta)
        
        return {
            "ok": True,
            "vault": safe_vault,
            "slideshow_count": len(valid_slideshow)
        }
    except Exception as ex:
        logger.error(f"Failed to update vault slideshow: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=400)
def _pg_read_vault_meta(db: Session, uid: str, name: str) -> dict:
    try:
        row = db.execute(text("SELECT metadata FROM public.vaults WHERE owner_uid=:uid AND name=:name"), {"uid": uid, "name": name}).first()
        if not row:
            return {}
        data = row[0]
        if isinstance(data, dict):
            return data
        try:
            return json.loads(data) if isinstance(data, str) else {}
        except Exception:
            return {}
    except Exception:
        return {}

def _pg_upsert_vault_meta(db: Session, uid: str, name: str, meta_updates: dict, visibility: str | None = None) -> None:
    md = json.dumps(meta_updates or {})
    try:
        existing = db.execute(text("SELECT id, visibility FROM public.vaults WHERE owner_uid=:uid AND name=:name"), {"uid": uid, "name": name}).first()
        if existing:
            vis = visibility or existing[1] or 'private'
            db.execute(text("UPDATE public.vaults SET metadata = COALESCE(metadata, '{}'::jsonb) || :md::jsonb, visibility=:vis, updated_at=NOW() WHERE owner_uid=:uid AND name=:name"), {"md": md, "vis": vis, "uid": uid, "name": name})
        else:
            vis = visibility or 'private'
            db.execute(text("INSERT INTO public.vaults (owner_uid, name, visibility, metadata) VALUES (:uid, :name, :vis, :md::jsonb)"), {"uid": uid, "name": name, "vis": vis, "md": md})
        db.commit()
    except Exception as ex:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(f"vault meta upsert failed: {ex}")


@router.get("/vaults/proofing/status")
async def get_proofing_status(request: Request, vault: str):
    """Check if client proofing is completed for a vault"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    try:
        # Read vault metadata to check for proofing completion
        meta = _read_vault_meta(uid, vault)
        proofing_complete = meta.get("proofing_complete", {})
        
        if not proofing_complete.get("completed", False):
            return {"completed": False}
        
        # Count approved images and retouch requests
        approvals = meta.get("approvals", {}).get("by_photo", {})
        approved_count = 0
        retouch_count = 0
        
        for photo_key, photo_data in approvals.items():
            by_email = photo_data.get("by_email", {})
            for email, email_data in by_email.items():
                if email_data.get("status") == "approved":
                    approved_count += 1
                    break  # Count each photo only once
        
        # Count retouch requests
        retouch_queue = meta.get("retouch_queue", [])
        retouch_count = len(retouch_queue)
        
        return {
            "completed": True,
            "approved_count": approved_count,
            "retouch_count": retouch_count,
            "completed_at": proofing_complete.get("completed_at")
        }
        
    except Exception as ex:
        logger.error(f"Failed to get proofing status: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=400)


class FinalDeliveryPayload(BaseModel):
    vault: str
    image_source: str  # 'approved' or 'edited'
    delivery_type: str  # 'download' or 'view_only'
    download_limit: Optional[int] = 1
    expiration_days: Optional[int] = 7
    zip_delivery: Optional[bool] = True


@router.post("/vaults/final-delivery")
async def prepare_final_delivery(request: Request, payload: FinalDeliveryPayload):
    """Prepare final delivery for completed proofing"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    try:
        vault = payload.vault.strip()
        if not vault:
            return JSONResponse({"error": "vault name required"}, status_code=400)
        
        # Read vault metadata
        meta = _read_vault_meta(uid, vault)
        
        # Verify proofing is completed
        proofing_complete = meta.get("proofing_complete", {})
        if not proofing_complete.get("completed", False):
            return JSONResponse({"error": "proofing not completed"}, status_code=400)
        
        # Prepare final delivery configuration
        final_delivery = {
            "prepared": True,
            "prepared_at": datetime.utcnow().isoformat(),
            "image_source": payload.image_source,
            "delivery_type": payload.delivery_type,
            "download_limit": payload.download_limit or 1,
            "expiration_days": payload.expiration_days or 7,
            "zip_delivery": payload.zip_delivery if payload.zip_delivery is not None else True,
            "downloads_used": 0
        }
        
        # Update vault metadata
        meta["final_delivery"] = final_delivery
        _write_vault_meta(uid, vault, meta)
        
        # Clear the proofing completion banner (it's served its purpose)
        if "proofing_complete" in meta:
            del meta["proofing_complete"]
            _write_vault_meta(uid, vault, meta)
        
        logger.info(f"Final delivery prepared for vault {vault} by {uid}")
        
        return {
            "ok": True,
            "vault": vault,
            "final_delivery": final_delivery
        }
        
    except Exception as ex:
        logger.error(f"Failed to prepare final delivery: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=400)