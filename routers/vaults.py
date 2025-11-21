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
from pathlib import Path
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Body, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from core.config import s3, s3_presign_client, R2_BUCKET, R2_PUBLIC_BASE_URL, R2_CUSTOM_DOMAIN, logger, DODO_API_BASE, DODO_CHECKOUT_PATH, DODO_PRODUCTS_PATH, DODO_API_KEY, DODO_WEBHOOK_SECRET, LICENSE_SECRET, LICENSE_PRIVATE_KEY, LICENSE_PUBLIC_KEY, LICENSE_ISSUER
from utils.storage import read_json_key, write_json_key, read_bytes_key, upload_bytes
from core.auth import get_uid_from_request, get_user_email_from_uid, get_fs_client
from utils.emailing import render_email, send_email_smtp
from utils.sendbird import create_vault_channel, ensure_sendbird_user, sendbird_api

router = APIRouter(prefix="/api", tags=["vaults"])


def _get_url_for_key(key: str, expires_in: int = 3600) -> str:
    """Generate URL for R2 key - prioritizes public URL over custom domain."""
    if R2_PUBLIC_BASE_URL:
        return f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}"
    if R2_CUSTOM_DOMAIN and s3_presign_client:
        return s3_presign_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": key},
            ExpiresIn=expires_in,
        )
    if s3:
        return s3.meta.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": key},
            ExpiresIn=expires_in,
        )
    return "" 

# Special vault machine name used historically for collaborator uploads
FRIENDS_VAULT_SAFE = "Photos_sent_by_friends" 

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

def _hash_password(pw: str, salt: str) -> str:
    try:
        return hashlib.sha256(((pw or '') + salt).encode('utf-8')).hexdigest()
    except Exception:
        return ''


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
    salt = _vault_salt(uid, vault)
    if meta.get('hash') == _hash_password(password or '', salt):
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
async def vaults_delete(request: Request, payload: dict = Body(...)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    vaults = payload.get("vaults", [])
    password = str(payload.get("password", "") or "").strip()
    
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
                
                stored_hash = meta.get("password_hash")
                if not stored_hash:
                    errors.append(name)
                    continue
                
                import hashlib
                attempt_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
                if attempt_hash != stored_hash:
                    return JSONResponse({"error": "Invalid password"}, status_code=403)
        except Exception:
            pass
        
        ok = _delete_vault(uid, name)
        if ok:
            deleted.append(name)
        else:
            errors.append(name)
    
    return {"deleted": deleted, "errors": errors}


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
async def vaults_create(request: Request, payload: VaultCreatePayload):
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
        
        return {
            "name": _vault_key(uid, name)[1], 
            "count": len(keys),
            "channel_url": channel_url
        }
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.post("/vaults/add")
async def vaults_add(request: Request, vault: str = Body(..., embed=True), keys: List[str] = Body(..., embed=True), password: Optional[str] = Body(None, embed=True)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Owner always has access to their own vaults, no password needed
    try:
        exist = _read_vault(uid, vault)
        filtered = [k for k in keys if k.startswith(f"users/{uid}/")]
        merged = sorted(set(exist) | set(filtered))
        _write_vault(uid, vault, merged)
        return {"vault": _vault_key(uid, vault)[1], "count": len(merged)}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.post("/vaults/upload")
async def vaults_upload(
    request: Request,
    files: List[UploadFile] = File(...),
    vault: str = Form(...),
    password: Optional[str] = Form(None)
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
    
    uploaded = []
    errors = []
    
    for uf in files:
        try:
            raw = await uf.read()
            if not raw:
                continue
            
            # Determine file extension
            orig_filename = uf.filename or 'image.jpg'
            ext = os.path.splitext(orig_filename)[1].lower()
            if not ext or ext not in ['.jpg', '.jpeg', '.png', '.webp', '.heic', '.tif', '.tiff', '.gif', '.cr2', '.cr3', '.nef', '.nrw', '.arw', '.sr2', '.srf', '.srw', '.orf', '.raf', '.rw2', '.rwl', '.pef', '.dng', '.3fr', '.erf', '.kdc', '.mrw', '.x3f', '.mef', '.iiq', '.fff']:
                ext = '.jpg'
            
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
async def vaults_remove(request: Request, vault: str = Body(..., embed=True), keys: List[str] = Body(..., embed=True), password: Optional[str] = Body(None, embed=True), delete_from_r2: Optional[bool] = Body(False, embed=True)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Owner always has access to their own vaults, no password needed
    try:
        exist = _read_vault(uid, vault)
        to_remove = set(keys)
        remain = [k for k in exist if k not in to_remove]
        _write_vault(uid, vault, remain)

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
    descriptions: Optional[dict[str, str]] | None = None


@router.post("/vaults/meta")
async def vaults_set_meta(request: Request, payload: VaultMetaUpdate):
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
        return {"ok": True, "vault": safe_vault, "display_name": meta.get("display_name"), "order": meta.get("order"), "share": {
            "hide_ui": bool(meta.get("share_hide_ui")),
            "color": str(meta.get("share_color") or ""),
            "layout": str(meta.get("share_layout") or "grid"),
            "logo_url": str(meta.get("share_logo_url") or ""),
        }}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


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
            
            salt = _vault_salt(uid, safe_vault)
            meta["protected"] = True
            meta["hash"] = _hash_password(payload.password, salt)
            
            # Lock the vault after adding protection
            _lock_vault(uid, safe_vault)
        else:
            # Removing protection
            meta["protected"] = False
            if "hash" in meta:
                del meta["hash"]
            
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


@router.get("/vaults/photos")
async def vaults_photos(request: Request, vault: str, password: Optional[str] = None):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Owner always has access to their own vaults, no password needed
    # Password protection only applies to shared access via tokens
    try:
        keys = _read_vault(uid, vault)
        # Hide collaborator-sent items from 'Photos sent by friends' vault
        try:
            if vault == FRIENDS_VAULT_SAFE:
                keys = [k for k in keys if ('/partners/' not in k and '-fromfriend' not in os.path.basename(k))]
        except Exception:
            pass
        # Apply optional explicit order from meta if present
        try:
            order = meta.get("order") if isinstance(meta, dict) else None
            if isinstance(order, list) and order:
                order_index = {k: i for i, k in enumerate(order)}
                keys = sorted(keys, key=lambda k: order_index.get(k, 10**9))
        except Exception:
            pass
        items: list[dict] = []
        if s3 and R2_BUCKET:
            # Build lookup of originals to attach to items
            orig_prefix = f"users/{uid}/originals/"
            original_lookup: dict[str, dict] = {}
            try:
                bucket = s3.Bucket(R2_BUCKET)
                for o in bucket.objects.filter(Prefix=orig_prefix):
                    ok = o.key
                    if ok.endswith("/"):
                        continue
                    o_url = _get_url_for_key(ok, expires_in=60 * 60)
                    original_lookup[ok] = {"url": o_url}
            except Exception:
                original_lookup = {}

            for key in keys:
                try:
                    # Skip accidental JSON sidecar entries
                    if key.lower().endswith('.json'):
                        continue
                    item = _make_item_from_key(uid, key)
                    name = os.path.basename(key)
                    original_key = None
                    # has_invisible is set inside _make_item_from_key via cache/detector
                    if "-o" in name:
                        try:
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
                        except Exception:
                            original_key = None
                    if original_key and original_key in original_lookup:
                        item["original_key"] = original_key
                        item["original_url"] = original_lookup[original_key]["url"]
                    # Attach optional friend note metadata if exists
                    try:
                        if "-fromfriend-" in name:
                            meta_key = f"{os.path.splitext(key)[0]}.json"
                            meta = read_json_key(meta_key)
                            if isinstance(meta, dict) and (meta.get("note") or meta.get("from")):
                                item["friend_note"] = str(meta.get("note") or "")
                                if meta.get("from"):
                                    item["friend_from"] = str(meta.get("from"))
                                if meta.get("at"):
                                    item["friend_at"] = str(meta.get("at"))
                    except Exception:
                        pass
                    items.append(item)
                except Exception:
                    # Best-effort fallback, also skip JSON sidecars
                    if not key.lower().endswith('.json'):
                        items.append(_make_item_from_key(uid, key))
        else:
            # Local storage: build a set of original keys available
            original_lookup: set[str] = set()
            orig_dir = os.path.join(STATIC_DIR, f"users/{uid}/originals/")
            if os.path.isdir(orig_dir):
                for root, _, files in os.walk(orig_dir):
                    for f in files:
                        rel = os.path.relpath(os.path.join(root, f), STATIC_DIR).replace("\\", "/")
                        original_lookup.add(rel)
            for key in keys:
                # Skip accidental JSON sidecar entries
                if key.lower().endswith('.json'):
                    continue
                item = _make_item_from_key(uid, key)
                try:
                    # has_invisible is set inside _make_item_from_key via cache/detector
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
                            item["original_key"] = cand
                            item["original_url"] = f"/static/{cand}"
                            break
                except Exception:
                    pass
                # Attach optional friend note metadata if exists
                try:
                    if "-fromfriend-" in name:
                        meta_key = f"{os.path.splitext(key)[0]}.json"
                        meta = read_json_key(meta_key)
                        if isinstance(meta, dict) and (meta.get("note") or meta.get("from")):
                            item["friend_note"] = str(meta.get("note") or "")
                            if meta.get("from"):
                                item["friend_from"] = str(meta.get("from"))
                            if meta.get("at"):
                                item["friend_at"] = str(meta.get("at"))
                except Exception:
                    pass
                items.append(item)
        return {"photos": items}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.post("/vaults/share")
async def vaults_share(request: Request, payload: dict = Body(...)):
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

    front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
    link = f"{front}/#share?token={token}"

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
                or data.get('businessName')
                or data.get('brand_name')
                or data.get('name')
                or data.get('displayName')
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

    subject = f"{studio_name} has shared photos for your review"

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
        f"{studio_name} has shared a set of photos with you to review and proof.<br><br>"
        f"You have been granted one-time access to view {count} {noun} in a secure photo vault.<br><br>"
        f"Click the link below to view your photos:<br>"
        f"<a href=\"{link}\">{link}</a><br><br>"
        f"This link will expire on: <strong>{expire_pretty}</strong>."
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
        + f"{studio_name} has shared a set of photos with you to review and proof.\n\n"
        + f"You have been granted one-time access to view {count} {noun} in a secure photo vault.\n\n"
        + "Click the link below to view your photos:\n"
        + f"{link}\n\n"
        + f"This link will expire on: {expire_pretty}."
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
async def vaults_share_link(request: Request, payload: dict = Body(...)):
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
    _write_json_key(_share_key(token), rec)

    front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
    link = f"{front}/#share?token={token}"
    return {"ok": True, "link": link, "token": token, "expires_at": expires_at_iso}


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
            if R2_PUBLIC_BASE_URL:
                for k in keys:
                    if k.lower().endswith('.json'):
                        continue
                    img_urls.append(_get_url_for_key(k, expires_in=60 * 60 * 24))
            else:
                # Signed URLs may expire; acceptable for immediate render
                for k in keys:
                    if k.lower().endswith('.json'):
                        continue
                    try:
                        url = s3.meta.client.generate_presigned_url(
                            "get_object", Params={"Bucket": R2_BUCKET, "Key": k}, ExpiresIn=60 * 60
                        )
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
        date_prefix = datetime.utcnow().strftime('%Y/%m/%d')
        key = f"users/{uid}/vaults/_meta/{safe_vault}/branding/{date_prefix}/logo{ext}"
        url = upload_bytes(key, data, content_type=ct)
        meta = _read_vault_meta(uid, safe_vault) or {}
        meta["share_logo_url"] = url
        _write_vault_meta(uid, safe_vault, meta)
        return {"ok": True, "logo_url": url}
    except Exception as ex:
        logger.warning(f"share logo upload failed: {ex}")
        return JSONResponse({"error": "upload failed"}, status_code=500)

@router.get("/vaults/shared/photos")
async def vaults_shared_photos(token: str, password: Optional[str] = None):
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
        # Verify password using the vault's hash
        salt = _vault_salt(uid, vault)
        provided_hash = _hash_password(password or '', salt)
        if provided_hash != meta.get('hash'):
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
                        if R2_PUBLIC_BASE_URL:
                            o_url = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{ok}"
                        elif R2_CUSTOM_DOMAIN and s3_presign_client:
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
        mmeta = _read_vault_meta(uid, vault) or {}
        share = {
            "hide_ui": bool(mmeta.get("share_hide_ui")),
            "color": str(mmeta.get("share_color") or ""),
            "layout": str(mmeta.get("share_layout") or "grid"),
            "logo_url": str(mmeta.get("share_logo_url") or ""),
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

    return {"photos": items, "vault": vault, "email": email, "approvals": approvals, "favorites": favorites, "licensed": licensed, "removal_unlocked": removal_unlocked, "requires_remove_password": bool((rec or {}).get("remove_pw_hash")), "price_cents": price_cents, "currency": currency, "share": share, "retouch": retouch}


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

    # Notify owner via email only for approvals (not denials)
    try:
        if action.startswith('approv'):
            owner_email = (get_user_email_from_uid(uid) or "").strip()
            if owner_email:
                name = os.path.basename(photo_key)
                client_display = client_name if client_name else client_email
                subject = f"{client_display} approved a photo in '{vault}'"
                intro = f"Client <strong>{client_display}</strong> <strong>approved</strong> the photo <strong>{name}</strong> in vault <strong>{vault}</strong>."
                if comment:
                    intro += f"<br>Comment: {comment}"
                html = render_email(
                    "email_basic.html",
                    title="Photo approved by client",
                    intro=intro,
                    button_label="Open Gallery",
                    button_url=(os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/") + "/#gallery",
                )
                text = f"{client_email} approved the photo {name} in vault '{vault}'."
                send_email_smtp(owner_email, subject, html, text)
    except Exception:
        pass

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

    # Notify owner via email (best-effort)
    try:
        owner_email = (get_user_email_from_uid(uid) or "").strip()
        if owner_email:
            name = os.path.basename(photo_key)
            subject = f"{client_email or 'A client'} requested a retouch in '{vault}'"
            intro = f"Client <strong>{client_email or 'unknown'}</strong> requested a <strong>retouch</strong> for photo <strong>{name}</strong> in vault <strong>{vault}</strong>."
            if comment:
                intro += f"<br>Details: {comment}"
            html = render_email(
                "email_basic.html",
                title="Retouch request received",
                intro=intro,
                button_label="Open Gallery",
                button_url=(os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/") + "/#gallery",
            )
            text = f"Retouch requested for {name} in vault '{vault}'. Comment: {comment}"
            send_email_smtp(owner_email, subject, html, text)
    except Exception:
        pass

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

    # Notify owner via email (best-effort)
    try:
        owner_email = (get_user_email_from_uid(uid) or "").strip()
        if owner_email and favorite:
            name = os.path.basename(photo_key)
            client_display = client_name if client_name else client_email
            subject = f"{client_display} favorited a photo in '{vault}'"
            intro = f"Client <strong>{client_display}</strong> <strong>favorited</strong> the photo <strong>{name}</strong> in vault <strong>{vault}</strong>."
            html = render_email(
                "email_basic.html",
                title="Client favorited a photo",
                intro=intro,
                button_label="Open Gallery",
                button_url=(os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/") + "/#gallery",
            )
            text = f"{client_email} favorited the photo {name} in vault '{vault}'."
            send_email_smtp(owner_email, subject, html, text)
    except Exception:
        pass

    return {"ok": True, "photo": photo_key, "favorite": favorite}


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
        # Overwrite object in-place so that existing keys/approvals remain intact
        try:
            upload_bytes(key, data, content_type=ct)
        except Exception as ex:
            logger.warning(f"retouch final upload failed for {key}: {ex}")
            return JSONResponse({"error": "upload failed"}, status_code=500)
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

    # Price and currency from vault meta
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
async def vaults_shared_originals_zip(token: str, password: Optional[str] = None):
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

    # Allow if licensed OR correct removal password provided
    allow_download = bool(rec.get("licensed"))
    if not allow_download:
        try:
            if rec.get("remove_pw_hash"):
                import hashlib
                salt = f"share::{token}"
                if hashlib.sha256(((password or '') + salt).encode('utf-8')).hexdigest() == rec.get("remove_pw_hash"):
                    allow_download = True
        except Exception:
            allow_download = False
    if not allow_download:
        return JSONResponse({"error": "not licensed"}, status_code=403)

    uid = rec.get('uid') or ''
    vault = rec.get('vault') or ''
    if not uid or not vault:
        return JSONResponse({"error": "invalid share"}, status_code=400)

    # Check if vault is protected - clients need password to access
    meta = _read_vault_meta(uid, vault)
    if meta.get('protected'):
        # Verify password using the vault's hash
        salt = _vault_salt(uid, vault)
        provided_hash = _hash_password(password or '', salt)
        if provided_hash != meta.get('hash'):
            return JSONResponse({"error": "Vault is protected. Invalid or missing password."}, status_code=403)

    # Collect vault keys and map to original keys
    try:
        keys = _read_vault(uid, vault)
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)

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
        for k in keys:
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

    # Build zip in-memory
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in original_items:
            zf.writestr(name, content)
    mem.seek(0)
    headers = {"Content-Disposition": f"attachment; filename=\"{vault}-originals.zip\""}
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
