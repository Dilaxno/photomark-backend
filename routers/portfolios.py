import os
import uuid
from typing import Optional, List
from fastapi import APIRouter, Request, UploadFile, File, Form, Depends
from fastapi.responses import JSONResponse

from core.config import logger
from core.auth import get_uid_from_request, get_user_email_from_uid
from utils.storage import write_json_key, read_json_key, upload_bytes, get_presigned_url
from utils.thumbnails import get_thumbnail_key
from utils.metadata import auto_embed_metadata_for_user
from models.gallery import GalleryAsset
from sqlalchemy.orm import Session
from sqlalchemy import text
from core.database import get_db
from models.user import User

router = APIRouter(prefix="/api", tags=["portfolios"])


def _items_key(uid: str) -> str:
  return f"portfolios/{uid}/items.json"


def _file_key(uid: str, pid: str, ext: str) -> str:
  return f"portfolios/{uid}/files/{pid}{ext}"


def _normalize_owner_slug(user: User) -> str:
  name = (user.display_name or '').strip().lower()
  slug_from_name = ''.join(ch if ch.isalnum() or ch == ' ' or ch == '-' else ' ' for ch in name).strip()
  slug_from_name = '-'.join([p for p in slug_from_name.split() if p])
  local = (user.email or '').split('@')[0].strip().lower()
  local_safe = ''.join(ch for ch in local if ch.isalnum() or ch == '-')
  return slug_from_name or local_safe or user.uid


# Neon PostgreSQL helpers

def _pg_ensure_table(db: Session):
  db.execute(text(
    """
    CREATE TABLE IF NOT EXISTS portfolio_items (
      id VARCHAR(64) PRIMARY KEY,
      uid VARCHAR(64) NOT NULL,
      key TEXT,
      image_url TEXT,
      title TEXT,
      description TEXT,
      taken_at DATE,
      created_at TIMESTAMPTZ DEFAULT NOW(),
      updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_portfolio_items_uid ON portfolio_items(uid);
    """
  ))
  db.commit()


def _pg_ensure_settings_table(db: Session):
  db.execute(text(
    """
    CREATE TABLE IF NOT EXISTS portfolio_settings (
      uid VARCHAR(64) PRIMARY KEY,
      data JSONB NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ DEFAULT NOW(),
      updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """
  ))
  db.commit()


def _pg_get_settings(db: Session, uid: str) -> Optional[dict]:
  import json
  _pg_ensure_settings_table(db)
  row = db.execute(text("SELECT data FROM portfolio_settings WHERE uid = :uid"), {"uid": uid}).mappings().first()
  if row:
    data = row.get("data")
    # Handle case where data might be a string or already a dict
    if isinstance(data, str):
      try:
        return json.loads(data)
      except Exception:
        return {}
    return data if isinstance(data, dict) else {}
  return None


def _pg_save_settings(db: Session, uid: str, data: dict):
  import json
  _pg_ensure_settings_table(db)
  # Serialize dict to JSON string for JSONB column
  data_json = json.dumps(data) if isinstance(data, dict) else '{}'
  db.execute(text(
    """
    INSERT INTO portfolio_settings (uid, data)
    VALUES (:uid, CAST(:data_json AS jsonb))
    ON CONFLICT (uid) DO UPDATE SET
      data = EXCLUDED.data,
      updated_at = NOW();
    """
  ), {"uid": uid, "data_json": data_json})
  db.commit()

def _pg_list_items(db: Session, uid: str) -> List[dict]:
  _pg_ensure_table(db)
  rows = db.execute(text("SELECT id, key, image_url, title, description, taken_at FROM portfolio_items WHERE uid = :uid ORDER BY created_at DESC"), {"uid": uid}).mappings().all()
  out: List[dict] = []
  for r in rows:
    out.append({
      "id": r.get("id"),
      "key": r.get("key"),
      "imageUrl": r.get("image_url"),
      "title": r.get("title") or "",
      "description": r.get("description") or "",
      "takenAt": r.get("taken_at").isoformat() if r.get("taken_at") else None,
    })
  return out

def _pg_upsert_item(db: Session, uid: str, item: dict):
  _pg_ensure_table(db)
  db.execute(text(
    """
    INSERT INTO portfolio_items (id, uid, key, image_url, title, description, taken_at)
    VALUES (:id, :uid, :key, :image_url, :title, :description, :taken_at)
    ON CONFLICT (id) DO UPDATE SET
      key = EXCLUDED.key,
      image_url = EXCLUDED.image_url,
      title = EXCLUDED.title,
      description = EXCLUDED.description,
      taken_at = EXCLUDED.taken_at,
      updated_at = NOW();
    """
  ), {
    "id": item.get("id"),
    "uid": uid,
    "key": item.get("key"),
    "image_url": item.get("imageUrl"),
    "title": item.get("title") or "",
    "description": item.get("description") or "",
    "taken_at": item.get("takenAt") or item.get("taken_at") or None,
  })
  db.commit()

def _pg_update_item_fields(db: Session, uid: str, pid: str, fields: dict) -> bool:
  _pg_ensure_table(db)
  res = db.execute(text(
    """
    UPDATE portfolio_items
    SET title = COALESCE(:title, title),
        description = COALESCE(:description, description),
        taken_at = COALESCE(:taken_at, taken_at),
        updated_at = NOW()
    WHERE id = :id AND uid = :uid
    """
  ), {
    "id": pid,
    "uid": uid,
    "title": fields.get("title"),
    "description": fields.get("description"),
    "taken_at": fields.get("takenAt") or fields.get("taken_at"),
  })
  db.commit()
  return res.rowcount > 0

def _pg_delete_item(db: Session, uid: str, pid: str) -> bool:
  _pg_ensure_table(db)
  res = db.execute(text("DELETE FROM portfolio_items WHERE id = :id AND uid = :uid"), {"id": pid, "uid": uid})
  db.commit()
  return res.rowcount > 0


@router.get("/portfolio")
async def get_portfolio(request: Request, db: Session = Depends(get_db)):
  uid = get_uid_from_request(request)
  if not uid:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)
  arr: List[dict] = _pg_list_items(db, uid)
  if not arr:
    items = read_json_key(_items_key(uid)) or {}
    arr = list(items.get("items") or [])
    for it in arr:
      try:
        _pg_upsert_item(db, uid, it)
      except Exception:
        pass
  out: List[dict] = []
  for it in arr:
    k = str(it.get("key") or "").strip()
    url = str(it.get("imageUrl") or "").strip()
    thumb_url = None
    taken = it.get("takenAt") or it.get("taken_at") or None
    if k:
      try:
        fresher = get_presigned_url(k, expires_in=60 * 60)
        if fresher:
          url = fresher
        # Try to get thumbnail URL
        thumb_key = get_thumbnail_key(k, 'small')
        thumb_url = get_presigned_url(thumb_key, expires_in=60 * 60)
      except Exception:
        pass
    out.append({
      "id": it.get("id"),
      "imageUrl": url,
      "thumbUrl": thumb_url,
      "title": it.get("title") or "",
      "description": it.get("description") or "",
      "takenAt": taken,
      "key": k if k else None,
    })
  return {"items": out}


@router.post("/portfolio/item")
async def create_item(request: Request, file: UploadFile = File(...), title: str = Form(""), description: str = Form(""), db: Session = Depends(get_db)):
  uid = get_uid_from_request(request)
  if not uid:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)
  try:
    raw = await file.read()
    if not raw:
      return JSONResponse({"error": "empty file"}, status_code=400)
    filename = file.filename or 'image.jpg'
    ext = os.path.splitext(filename)[1].lower() or '.jpg'
    
    # Auto-embed IPTC/EXIF metadata if user has it enabled
    try:
      raw = auto_embed_metadata_for_user(raw, uid)
    except Exception as meta_ex:
      logger.debug(f"Metadata embed skipped: {meta_ex}")
    
    pid = uuid.uuid4().hex[:12]
    key = _file_key(uid, pid, ext)
    url = upload_bytes(key, raw, content_type={
      '.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.webp':'image/webp','.heic':'image/heic','.tif':'image/tiff','.tiff':'image/tiff'
    }.get(ext, 'application/octet-stream'))
    rec = {"id": pid, "key": key, "imageUrl": url, "title": title or '', "description": description or '', "takenAt": None}
    doc = read_json_key(_items_key(uid)) or {}
    arr: List[dict] = list(doc.get('items') or [])
    arr.insert(0, rec)
    write_json_key(_items_key(uid), {"items": arr})
    try:
      _pg_upsert_item(db, uid, rec)
    except Exception:
      pass
    try:
      ex = db.query(GalleryAsset).filter(GalleryAsset.key == key).first()
      if ex:
        ex.user_uid = uid
        ex.vault = None
        ex.size_bytes = len(raw)
      else:
        db.add(GalleryAsset(user_uid=uid, vault=None, key=key, size_bytes=len(raw)))
      db.commit()
    except Exception:
      try:
        db.rollback()
      except Exception:
        pass
    return {"item": rec}
  except Exception as ex:
    logger.warning(f"portfolio create_item failed: {ex}")
    return JSONResponse({"error": "upload failed"}, status_code=500)


@router.put("/portfolio/item/{pid}")
async def update_item(request: Request, pid: str, payload: dict, db: Session = Depends(get_db)):
  uid = get_uid_from_request(request)
  if not uid:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)
  # Try Neon first
  try:
    ok = _pg_update_item_fields(db, uid, pid, payload)
    if ok:
      return {"ok": True}
  except Exception:
    pass
  doc = read_json_key(_items_key(uid)) or {}
  arr: List[dict] = list(doc.get('items') or [])
  changed = False
  for it in arr:
    if str(it.get('id') or '') == pid:
      if 'title' in payload:
        it['title'] = str(payload.get('title') or '')
      if 'description' in payload:
        it['description'] = str(payload.get('description') or '')
      if 'takenAt' in payload or 'taken_at' in payload:
        val = payload.get('takenAt') if 'takenAt' in payload else payload.get('taken_at')
        it['takenAt'] = str(val or '')
      changed = True
      try:
        _pg_upsert_item(db, uid, it)
      except Exception:
        pass
      break
  if not changed:
    return JSONResponse({"error": "not found"}, status_code=404)
  try:
    write_json_key(_items_key(uid), {"items": arr})
    return {"ok": True}
  except Exception:
    return JSONResponse({"error": "update failed"}, status_code=500)


@router.delete("/portfolio/item/{pid}")
async def delete_item(request: Request, pid: str, db: Session = Depends(get_db)):
  uid = get_uid_from_request(request)
  if not uid:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)
  try:
    _pg_delete_item(db, uid, pid)
  except Exception:
    pass
  doc = read_json_key(_items_key(uid)) or {}
  arr: List[dict] = list(doc.get('items') or [])
  next_arr = [it for it in arr if str(it.get('id') or '') != pid]
  if len(next_arr) == len(arr):
    return JSONResponse({"error": "not found"}, status_code=404)
  try:
    write_json_key(_items_key(uid), {"items": next_arr})
    return {"ok": True}
  except Exception:
    return JSONResponse({"error": "delete failed"}, status_code=500)


def _find_uid_by_owner_slug(db: Session, owner: str) -> Optional[str]:
  """Helper to find user UID by owner slug."""
  q = (owner or '').strip().lower()
  if not q:
    return None
  users = db.query(User).all()
  # Try display_name normalized
  for u in users:
    slug = _normalize_owner_slug(u)
    if slug == q:
      return u.uid
  # Try email local-part match
  for u in users:
    local = (u.email or '').split('@')[0].strip().lower()
    if local == q:
      return u.uid
  return None


@router.get("/portfolios/{owner}")
async def public_portfolio(owner: str, db: Session = Depends(get_db)):
  try:
    q = (owner or '').strip().lower()
    if not q:
      return JSONResponse({"error": "owner required"}, status_code=400)
    target_uid = _find_uid_by_owner_slug(db, owner)
    if not target_uid:
      return JSONResponse({"error": "not found"}, status_code=404)
    arr: List[dict] = _pg_list_items(db, target_uid)
    if not arr:
      doc = read_json_key(_items_key(target_uid)) or {}
      arr = list(doc.get('items') or [])
    out: List[dict] = []
    for it in arr:
      k = str(it.get("key") or "").strip()
      url = str(it.get("imageUrl") or "").strip()
      thumb_url = None
      taken = it.get("takenAt") or it.get("taken_at") or None
      if k:
        try:
          fresher = get_presigned_url(k, expires_in=60 * 60)
          if fresher:
            url = fresher
          # Try to get thumbnail URL
          thumb_key = get_thumbnail_key(k, 'small')
          thumb_url = get_presigned_url(thumb_key, expires_in=60 * 60)
        except Exception:
          pass
      out.append({
        "id": it.get("id"),
        "imageUrl": url,
        "thumbUrl": thumb_url,
        "title": it.get("title") or "",
        "description": it.get("description") or "",
        "takenAt": taken,
      })
    return {"items": out}
  except Exception as ex:
    logger.warning(f"public_portfolio failed: {ex}")
    return JSONResponse({"error": "server error"}, status_code=500)


@router.get("/portfolios/{owner}/settings")
async def public_portfolio_settings(owner: str, db: Session = Depends(get_db)):
  """Get portfolio settings for a public portfolio by owner slug."""
  try:
    q = (owner or '').strip().lower()
    if not q:
      return JSONResponse({"error": "owner required"}, status_code=400)
    target_uid = _find_uid_by_owner_slug(db, owner)
    if not target_uid:
      return JSONResponse({"error": "not found"}, status_code=404)
    data = _pg_get_settings(db, target_uid)
    return {"ok": True, "data": data}
  except Exception as ex:
    logger.warning(f"public_portfolio_settings failed: {ex}")
    return JSONResponse({"error": "server error"}, status_code=500)


@router.post("/portfolio/items/by_keys")
async def add_items_by_keys(request: Request, payload: dict, db: Session = Depends(get_db)):
  uid = get_uid_from_request(request)
  if not uid:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)
  try:
    keys = payload.get("keys") or []
    if not isinstance(keys, list) or not keys:
      return JSONResponse({"error": "keys required"}, status_code=400)
    items = read_json_key(_items_key(uid)) or {}
    arr: List[dict] = list(items.get("items") or [])
    added = 0
    for k in keys:
      try:
        ks = str(k or "").strip()
        if not ks:
          continue
        pid = uuid.uuid4().hex[:12]
        url = get_presigned_url(ks, expires_in=60 * 60) or f"/static/{ks}"
        title = os.path.basename(ks)
        rec = {"id": pid, "key": ks, "imageUrl": url, "title": title, "description": "", "takenAt": None}
        arr.insert(0, rec)
        try:
          _pg_upsert_item(db, uid, rec)
        except Exception:
          pass
        added += 1
      except Exception:
        continue
    write_json_key(_items_key(uid), {"items": arr})
    return {"ok": True, "added": added}
  except Exception as ex:
    logger.warning(f"add_items_by_keys failed: {ex}")
    return JSONResponse({"error": "server error"}, status_code=500)


# Portfolio Builder Settings Endpoints

@router.get("/portfolio/builder/settings")
async def get_builder_settings(request: Request, db: Session = Depends(get_db)):
  """Get portfolio builder settings for the authenticated user."""
  uid = get_uid_from_request(request)
  if not uid:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)
  try:
    data = _pg_get_settings(db, uid)
    return {"ok": True, "data": data}
  except Exception as ex:
    logger.warning(f"get_builder_settings failed: {ex}")
    return JSONResponse({"error": "server error"}, status_code=500)


@router.post("/portfolio/builder/settings")
async def save_builder_settings(request: Request, payload: dict, db: Session = Depends(get_db)):
  """Save portfolio builder settings for the authenticated user."""
  uid = get_uid_from_request(request)
  if not uid:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)
  try:
    # Validate payload structure
    data = payload.get("data")
    if not isinstance(data, dict):
      return JSONResponse({"error": "Invalid data format"}, status_code=400)
    
    # Save to database
    _pg_save_settings(db, uid, data)
    return {"ok": True}
  except Exception as ex:
    logger.warning(f"save_builder_settings failed: {ex}")
    return JSONResponse({"error": "server error"}, status_code=500)
