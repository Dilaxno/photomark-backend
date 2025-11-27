import os
import uuid
from typing import Optional, List
from fastapi import APIRouter, Request, UploadFile, File, Form, Depends
from fastapi.responses import JSONResponse

from core.config import logger
from core.auth import get_uid_from_request, get_user_email_from_uid
from utils.storage import write_json_key, read_json_key, upload_bytes, get_presigned_url
from sqlalchemy.orm import Session
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


@router.get("/portfolio")
async def get_portfolio(request: Request):
  uid = get_uid_from_request(request)
  if not uid:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)
  items = read_json_key(_items_key(uid)) or {}
  arr: List[dict] = list(items.get("items") or [])
  out: List[dict] = []
  for it in arr:
    k = str(it.get("key") or "").strip()
    url = str(it.get("imageUrl") or "").strip()
    if k:
      try:
        fresher = get_presigned_url(k, expires_in=60 * 60)
        if fresher:
          url = fresher
      except Exception:
        pass
    out.append({
      "id": it.get("id"),
      "imageUrl": url,
      "title": it.get("title") or "",
      "description": it.get("description") or "",
      "key": k if k else None,
    })
  return {"items": out}


@router.post("/portfolio/item")
async def create_item(request: Request, file: UploadFile = File(...), title: str = Form(""), description: str = Form("")):
  uid = get_uid_from_request(request)
  if not uid:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)
  try:
    raw = await file.read()
    if not raw:
      return JSONResponse({"error": "empty file"}, status_code=400)
    filename = file.filename or 'image.jpg'
    ext = os.path.splitext(filename)[1].lower() or '.jpg'
    pid = uuid.uuid4().hex[:12]
    key = _file_key(uid, pid, ext)
    url = upload_bytes(key, raw, content_type={
      '.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.webp':'image/webp','.heic':'image/heic','.tif':'image/tiff','.tiff':'image/tiff'
    }.get(ext, 'application/octet-stream'))
    rec = {"id": pid, "key": key, "imageUrl": url, "title": title or '', "description": description or ''}
    doc = read_json_key(_items_key(uid)) or {}
    arr: List[dict] = list(doc.get('items') or [])
    arr.insert(0, rec)
    write_json_key(_items_key(uid), {"items": arr})
    return {"item": rec}
  except Exception as ex:
    logger.warning(f"portfolio create_item failed: {ex}")
    return JSONResponse({"error": "upload failed"}, status_code=500)


@router.put("/portfolio/item/{pid}")
async def update_item(request: Request, pid: str, payload: dict):
  uid = get_uid_from_request(request)
  if not uid:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)
  doc = read_json_key(_items_key(uid)) or {}
  arr: List[dict] = list(doc.get('items') or [])
  changed = False
  for it in arr:
    if str(it.get('id') or '') == pid:
      if 'title' in payload:
        it['title'] = str(payload.get('title') or '')
      if 'description' in payload:
        it['description'] = str(payload.get('description') or '')
      changed = True
      break
  if not changed:
    return JSONResponse({"error": "not found"}, status_code=404)
  try:
    write_json_key(_items_key(uid), {"items": arr})
    return {"ok": True}
  except Exception:
    return JSONResponse({"error": "update failed"}, status_code=500)


@router.delete("/portfolio/item/{pid}")
async def delete_item(request: Request, pid: str):
  uid = get_uid_from_request(request)
  if not uid:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)
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


@router.get("/portfolios/{owner}")
async def public_portfolio(owner: str, db: Session = Depends(get_db)):
  try:
    user = None
    q = (owner or '').strip().lower()
    if not q:
      return JSONResponse({"error": "owner required"}, status_code=400)
    # Try display_name normalized
    users = db.query(User).all()
    target_uid: Optional[str] = None
    for u in users:
      slug = _normalize_owner_slug(u)
      if slug == q:
        target_uid = u.uid
        break
    if not target_uid:
      # Try email local-part match
      for u in users:
        local = (u.email or '').split('@')[0].strip().lower()
        if local == q:
          target_uid = u.uid
          break
    if not target_uid:
      return JSONResponse({"error": "not found"}, status_code=404)
    doc = read_json_key(_items_key(target_uid)) or {}
    arr: List[dict] = list(doc.get('items') or [])
    out: List[dict] = []
    for it in arr:
      k = str(it.get("key") or "").strip()
      url = str(it.get("imageUrl") or "").strip()
      if k:
        try:
          fresher = get_presigned_url(k, expires_in=60 * 60)
          if fresher:
            url = fresher
        except Exception:
          pass
      out.append({
        "id": it.get("id"),
        "imageUrl": url,
        "title": it.get("title") or "",
        "description": it.get("description") or "",
      })
    return {"items": out}
  except Exception as ex:
    logger.warning(f"public_portfolio failed: {ex}")
    return JSONResponse({"error": "server error"}, status_code=500)
