import os
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Request, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.config import logger, s3, R2_BUCKET, ADMIN_ALLOWLIST_IPS
from sqlalchemy.orm import Session
from core.database import get_db
from models.user import User
from routers.vaults import _read_vault_meta, _vault_key, STATIC_DIR, _pg_upsert_vault_meta

router = APIRouter(prefix="/api/admin", tags=["admin"])  # secure endpoints via ADMIN_SECRET


# --- Security helpers ---

def _get_admin_secret() -> str:
    return (os.getenv("ADMIN_SECRET") or "").strip()


def _extract_secret(request: Request, explicit: Optional[str] = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    # Header takes precedence if provided
    try:
      h = request.headers.get("X-Admin-Secret", "").strip()
      if h:
        return h
    except Exception:
      pass
    # Query fallback
    try:
      q = request.query_params.get("secret", "").strip()
      if q:
        return q
    except Exception:
      pass
    return ""


def _require_admin(request: Request, secret: Optional[str] = None) -> Optional[JSONResponse]:
    configured = _get_admin_secret()
    if not configured:
        return JSONResponse({"error": "admin_not_configured"}, status_code=503)
    provided = _extract_secret(request, secret)
    if not provided or provided != configured:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        if ADMIN_ALLOWLIST_IPS:
            ip = request.client.host if request.client else ""
            forwarded = request.headers.get("X-Forwarded-For")
            if forwarded:
                ip = forwarded.split(",")[0].strip()
            if ip and ip not in ADMIN_ALLOWLIST_IPS:
                return JSONResponse({"error": "forbidden"}, status_code=403)
    except Exception:
        pass
    return None


# --- Models ---

class UpdateUserPayload(BaseModel):
    uid: str
    updates: Dict[str, Any]


class DeleteUserPayload(BaseModel):
    uid: str
    delete_storage: Optional[bool] = False


class BatchDeletePayload(BaseModel):
    uids: List[str]
    delete_storage: Optional[bool] = False


class BatchUpdatePayload(BaseModel):
    uids: List[str]
    updates: Dict[str, Any]


# --- User helpers (search fields) ---

_USER_FIELDS_INDEX = (
    "email",
    "name",
    "displayName",
    "studioName",
    "businessName",
    "brand_name",
    "handle",
    "role",
)


def _fetch_all_users_sql(db: Session, max_count: int = 1000) -> List[Tuple[str, Dict[str, Any]]]:
    rows = (
        db.query(User)
        .order_by(User.created_at.desc())
        .limit(max(1, min(int(max_count), 5000)))
        .all()
    )
    out: List[Tuple[str, Dict[str, Any]]] = []
    for u in rows:
        try:
            data = {
                "email": u.email,
                "name": u.display_name,
                "displayName": u.display_name,
                "is_active": u.is_active,
                "plan": u.plan,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "updated_at": u.updated_at.isoformat() if u.updated_at else None,
                "last_login": u.last_login_at.isoformat() if u.last_login_at else None,
            }
            out.append((u.uid, data))
        except Exception:
            continue
    return out


def _build_search_blob(uid: str, data: Dict[str, Any]) -> str:
    parts: List[str] = [uid]
    for f in _USER_FIELDS_INDEX:
        v = data.get(f)
        if v is not None:
            try:
                parts.append(str(v))
            except Exception:
                continue
    # Include any known contact/meta fields if present
    for k in ("phone", "company", "website", "bio"):
        v = data.get(k)
        if v is not None:
            try:
                parts.append(str(v))
            except Exception:
                pass
    blob = " ".join(parts)
    blob = re.sub(r"\s+", " ", blob).strip().lower()
    return blob


def _similarity(a: str, b: str) -> float:
    # Lightweight semantic-ish similarity: token overlap + partial ratio
    if not a or not b:
        return 0.0
    a = a.lower().strip()
    b = b.lower().strip()
    if not a or not b:
        return 0.0
    # Token overlap score (Jaccard)
    at = set(re.split(r"[^a-z0-9@._+-]+", a)) - {""}
    bt = set(re.split(r"[^a-z0-9@._+-]+", b)) - {""}
    inter = len(at & bt)
    union = len(at | bt) or 1
    jacc = inter / union
    # Partial ratio using simple longest common substring heuristic (bounded)
    best = 0
    la, lb = len(a), len(b)
    for i in range(min(la, 64)):
        for j in range(min(lb, 64)):
            k = 0
            while i + k < la and j + k < lb and a[i + k] == b[j + k] and k < 48:
                k += 1
            if k > best:
                best = k
    partial = best / max(1, min(len(a), len(b)))
    # Weighted combo
    return 0.65 * jacc + 0.35 * partial


# --- Endpoints ---

@router.get("/users")
async def admin_users_list(request: Request, secret: Optional[str] = None, limit: Optional[int] = 1000, db: Session = Depends(get_db)):
    sec = _require_admin(request, secret)
    if sec is not None:
        return sec
    try:
        lim = int(limit or 1000)
        users = _fetch_all_users_sql(db, max_count=max(1, min(lim, 5000)))
        out = [
            {
                "uid": uid,
                "data": data,
                "summary": {
                    "email": str(data.get("email") or ""),
                    "name": str(data.get("name") or data.get("displayName") or ""),
                    "studio": str(data.get("studioName") or data.get("businessName") or data.get("brand_name") or ""),
                    "role": str(data.get("role") or ""),
                    "active": bool(data.get("is_active", True)),
                    "created_at": str(data.get("created_at") or data.get("createdAt") or ""),
                    "updated_at": str(data.get("updated_at") or data.get("updatedAt") or ""),
                    "last_login": str(data.get("last_login") or data.get("lastLogin") or ""),
                },
            }
            for uid, data in users
        ]
        return {"users": out}
    except Exception as ex:
        logger.warning(f"/api/admin/users failed: {ex}")
        return JSONResponse({"error": "list_failed"}, status_code=500)


@router.get("/users/search")
async def admin_users_search(request: Request, q: str, secret: Optional[str] = None, limit: Optional[int] = 200, db: Session = Depends(get_db)):
    sec = _require_admin(request, secret)
    if sec is not None:
        return sec
    query = (q or "").strip()
    if not query:
        return JSONResponse({"error": "q_required"}, status_code=400)
    try:
        users = _fetch_all_users_sql(db, max_count=5000)
        qb = query.lower().strip()
        scored: List[Tuple[float, Tuple[str, Dict[str, Any]]]] = []
        for uid, data in users:
            blob = _build_search_blob(uid, data)
            score = _similarity(qb, blob)
            # Boost exact partial matches on key fields
            try:
                for f in _USER_FIELDS_INDEX:
                    v = str(data.get(f) or "").lower()
                    if v and qb in v:
                        score += 0.3
            except Exception:
                pass
            if score > 0:
                scored.append((min(score, 1.0), (uid, data)))
        scored.sort(key=lambda x: x[0], reverse=True)
        lim = int(limit or 200)
        out = [
            {
                "uid": uid,
                "score": round(sc, 4),
                "data": data,
                "summary": {
                    "email": str(data.get("email") or ""),
                    "name": str(data.get("name") or data.get("displayName") or ""),
                    "studio": str(data.get("studioName") or data.get("businessName") or data.get("brand_name") or ""),
                    "role": str(data.get("role") or ""),
                    "active": bool(data.get("is_active", True)),
                },
            }
            for sc, (uid, data) in scored[: max(1, min(lim, 1000))]
        ]
        return {"users": out, "query": query}
    except Exception as ex:
        logger.warning(f"/api/admin/users/search failed: {ex}")
    return JSONResponse({"error": "search_failed"}, status_code=500)


def _list_vault_names_for_uid(uid: str) -> List[str]:
    prefix = f"users/{uid}/vaults/"
    names: List[str] = []
    try:
        if s3 and R2_BUCKET:
            bucket = s3.Bucket(R2_BUCKET)
            for obj in bucket.objects.filter(Prefix=prefix):
                key = obj.key
                if not key.endswith(".json"):
                    continue
                tail = key[len(prefix):]
                if "/" in tail:
                    continue
                base = os.path.basename(key)[:-5]
                names.append(base)
        else:
            dir_path = os.path.join(STATIC_DIR, prefix)
            if os.path.isdir(dir_path):
                for f in os.listdir(dir_path):
                    if f.endswith(".json") and f != "_meta.json":
                        names.append(f[:-5])
    except Exception:
        pass
    out = sorted(set(n for n in names if isinstance(n, str) and n.strip()))
    return out


def _shared_pairs_from_tokens() -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    try:
        if s3 and R2_BUCKET:
            bucket = s3.Bucket(R2_BUCKET)
            for obj in bucket.objects.filter(Prefix="shares/"):
                k = obj.key
                if not k.endswith(".json"):
                    continue
                try:
                    import json as _j
                    body = obj.get()["Body"].read().decode("utf-8")
                    rec = _j.loads(body)
                    uid = str(rec.get("uid") or "").strip()
                    vault = str(rec.get("vault") or "").strip()
                    if uid and vault:
                        pairs.add((uid, vault))
                except Exception:
                    continue
        else:
            base = os.path.join(STATIC_DIR, "shares")
            if os.path.isdir(base):
                for f in os.listdir(base):
                    if f.endswith(".json"):
                        try:
                            path = os.path.join(base, f)
                            with open(path, "r", encoding="utf-8") as fh:
                                rec = json.load(fh)
                            uid = str(rec.get("uid") or "").strip()
                            vault = str(rec.get("vault") or "").strip()
                            if uid and vault:
                                pairs.add((uid, vault))
                        except Exception:
                            continue
    except Exception:
        pass
    return pairs


@router.post("/migrate/vaults-meta")
async def admin_migrate_vaults_meta(request: Request, secret: Optional[str] = None, db: Session = Depends(get_db)):
    sec = _require_admin(request, secret)
    if sec is not None:
        return sec
    try:
        users = _fetch_all_users_sql(db, max_count=5000)
        shared_pairs = _shared_pairs_from_tokens()
        migrated = 0
        skipped = 0
        errors: List[str] = []
        for uid, _ in users:
            try:
                vault_names = _list_vault_names_for_uid(uid)
                for v in vault_names:
                    try:
                        safe_v = _vault_key(uid, v)[1]
                        meta = _read_vault_meta(uid, safe_v) or {}
                        vis = "shared" if (uid, safe_v) in shared_pairs else "private"
                        _pg_upsert_vault_meta(db, uid, safe_v, meta, visibility=vis)
                        migrated += 1
                    except Exception as ex:
                        errors.append(f"{uid}:{v}:{ex}")
                        skipped += 1
            except Exception as ex:
                errors.append(f"{uid}:list_failed:{ex}")
                continue
        return {"ok": True, "migrated": migrated, "skipped": skipped, "errors": errors[:50]}
    except Exception as ex:
        logger.warning(f"/api/admin/migrate/vaults-meta failed: {ex}")
        return JSONResponse({"error": "migrate_failed"}, status_code=500)


@router.post("/users/update")
async def admin_user_update(request: Request, payload: UpdateUserPayload, secret: Optional[str] = None, db: Session = Depends(get_db)):
    sec = _require_admin(request, secret)
    if sec is not None:
        return sec
    uid = (payload.uid or "").strip()
    if not uid:
        return JSONResponse({"error": "uid_required"}, status_code=400)
    updates = dict(payload.updates or {})
    if not updates:
        return JSONResponse({"error": "no_updates"}, status_code=400)
    try:
        u = db.query(User).filter(User.uid == uid).first()
        if not u:
            return JSONResponse({"error": "not_found"}, status_code=404)
        # Map known fields
        mapping = {
            "email": "email",
            "name": "display_name",
            "displayName": "display_name",
            "plan": "plan",
            "is_active": "is_active",
            "email_verified": "email_verified",
        }
        for k, v in (updates or {}).items():
            attr = mapping.get(k)
            if not attr:
                continue
            try:
                setattr(u, attr, v)
            except Exception:
                continue
        db.commit()
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"/api/admin/users/update failed: {ex}")
        return JSONResponse({"error": "update_failed"}, status_code=500)


@router.post("/users/delete")
async def admin_user_delete(request: Request, payload: DeleteUserPayload, secret: Optional[str] = None, db: Session = Depends(get_db)):
    sec = _require_admin(request, secret)
    if sec is not None:
        return sec
    uid = (payload.uid or "").strip()
    if not uid:
        return JSONResponse({"error": "uid_required"}, status_code=400)
    try:
        # Delete user from PostgreSQL
        try:
            u = db.query(User).filter(User.uid == uid).first()
            if u:
                db.delete(u)
                db.commit()
        except Exception:
            db.rollback()
        # Optionally delete storage under users/{uid}/
        deleted_storage = []
        if payload.delete_storage and s3 and R2_BUCKET:
            try:
                bucket = s3.Bucket(R2_BUCKET)
                prefix = f"users/{uid}/"
                objs = list(bucket.objects.filter(Prefix=prefix))
                batch = [{"Key": o.key} for o in objs if o.key and not o.key.endswith("/")]
                if batch:
                    resp = bucket.delete_objects(Delete={"Objects": batch, "Quiet": True})
                    for d in (resp or {}).get("Deleted", []):
                        k = d.get("Key")
                        if k:
                            deleted_storage.append(k)
            except Exception as ex:
                logger.warning(f"storage cleanup failed for {uid}: {ex}")
        return {"ok": True, "uid": uid, "storage_deleted": len(deleted_storage)}
    except Exception as ex:
        logger.warning(f"/api/admin/users/delete failed: {ex}")
        return JSONResponse({"error": "delete_failed"}, status_code=500)


@router.post("/users/batch_delete")
async def admin_users_batch_delete(request: Request, payload: BatchDeletePayload, secret: Optional[str] = None):
    sec = _require_admin(request, secret)
    if sec is not None:
        return sec
    uids = [str(u or "").strip() for u in (payload.uids or []) if str(u or "").strip()]
    if not uids:
        return JSONResponse({"error": "uids_required"}, status_code=400)
    ok: List[str] = []
    errors: List[str] = []
    for uid in uids:
        try:
            res = await admin_user_delete(request, DeleteUserPayload(uid=uid, delete_storage=payload.delete_storage), secret)
            if isinstance(res, JSONResponse):
                errors.append(f"{uid} : {getattr(res, 'status_code', 500)}")
            else:
                ok.append(uid)
        except Exception as ex:
            errors.append(f"{uid} : {ex}")
    return {"ok": True, "deleted": ok, "errors": errors}


@router.post("/users/batch_update")
async def admin_users_batch_update(request: Request, payload: BatchUpdatePayload, secret: Optional[str] = None):
    sec = _require_admin(request, secret)
    if sec is not None:
        return sec
    uids = [str(u or "").strip() for u in (payload.uids or []) if str(u or "").strip()]
    updates = dict(payload.updates or {})
    if not uids:
        return JSONResponse({"error": "uids_required"}, status_code=400)
    if not updates:
        return JSONResponse({"error": "no_updates"}, status_code=400)
    ok: List[str] = []
    errors: List[str] = []
    try:
        db = get_fs_client()
        if not db:
            return JSONResponse({"error": "no_firestore"}, status_code=503)
        for uid in uids:
            try:
                db.collection("users").document(uid).set(updates, merge=True)
                ok.append(uid)
            except Exception as ex:
                errors.append(f"{uid} : {ex}")
        return {"ok": True, "updated": ok, "errors": errors}
    except Exception as ex:
        logger.warning(f"/api/admin/users/batch_update failed: {ex}")
        return JSONResponse({"error": "batch_update_failed"}, status_code=500)
