from fastapi import APIRouter, Request, Depends, HTTPException, Body
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from datetime import datetime
import os

from core.auth import get_uid_from_request
from core.database import get_db
from models.user import User
from models.gallery import GalleryAsset
from core.config import s3, R2_BUCKET, STATIC_DIR as static_dir

router = APIRouter(prefix="/api/billing", tags=["billing"])


@router.get("/info")
async def billing_info(request: Request, db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        user = db.query(User).filter(User.uid == uid).first()
        if not user:
            return JSONResponse({"error": "User not found"}, status_code=404)

        plan = user.plan or "free"
        details = user.extra_metadata or {}

        # Prefer explicit next billing from metadata; fallback to subscription_end_date
        next_billing_iso = None
        try:
            nb = details.get("nextBillingAt")
            if isinstance(nb, str) and nb:
                next_billing_iso = datetime.fromisoformat(nb.replace('Z', '+00:00')).isoformat()
        except Exception:
            next_billing_iso = None
        if not next_billing_iso and getattr(user, "subscription_end_date", None):
            try:
                next_billing_iso = user.subscription_end_date.isoformat()
            except Exception:
                next_billing_iso = None
        try:
            if plan and plan != "free":
                from datetime import timedelta
                interval = str(details.get("interval") or "month").lower()
                now = datetime.utcnow()
                if next_billing_iso:
                    try:
                        parsed = datetime.fromisoformat(next_billing_iso.replace('Z', '+00:00'))
                        if parsed < now:
                            delta = timedelta(days=365) if interval == "year" else timedelta(days=30)
                            next_billing_iso = (now + delta).replace(microsecond=0).isoformat()
                    except Exception:
                        next_billing_iso = None
                if not next_billing_iso:
                    delta = timedelta(days=365) if interval == "year" else timedelta(days=30)
                    next_billing_iso = (now + delta).replace(microsecond=0).isoformat()
        except Exception:
            pass

        member_since_iso = None
        try:
            member_since_iso = user.created_at.isoformat() if user.created_at else None
        except Exception:
            member_since_iso = None

        billing = {
            "plan": plan,
            "subscriptionId": user.subscription_id,
            "nextBillingAt": next_billing_iso,
            "memberSince": member_since_iso,
            "status": (user.subscription_status or details.get("status") or ("active" if plan != "free" else "inactive")),
            "currency": details.get("currency", "USD"),
            "price": details.get("price"),
            "interval": details.get("interval", "month"),
            "paymentMethod": details.get("paymentMethod"),
        }
        return {"billing": billing}
    except HTTPException:
        raise
    except Exception as ex:
        return JSONResponse({"error": f"Failed to fetch billing info: {ex}"}, status_code=500)


@router.get("/usage")
async def billing_usage(request: Request, db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        user = db.query(User).filter(User.uid == uid).first()
        if not user:
            return JSONResponse({"error": "User not found"}, status_code=404)

        plan = (user.plan or "free").strip().lower()
        count = db.query(GalleryAsset).filter(GalleryAsset.user_uid == uid).count()
        try:
            total_bytes = int(
                db.query(GalleryAsset)
                  .filter(GalleryAsset.user_uid == uid)
                  .with_entities(func.coalesce(func.sum(GalleryAsset.size_bytes), 0))
                  .scalar() or 0
            )
        except Exception:
            total_bytes = 0
        try:
            if int(total_bytes) == 0 or int(count) == 0:
                _backfill_user_storage(db, uid)
                count = db.query(GalleryAsset).filter(GalleryAsset.user_uid == uid).count()
                total_bytes = int(
                    db.query(GalleryAsset)
                      .filter(GalleryAsset.user_uid == uid)
                      .with_entities(func.coalesce(func.sum(GalleryAsset.size_bytes), 0))
                      .scalar() or 0
                )
        except Exception:
            pass

        # Plan storage limits
        limit_bytes = None
        unlimited = False
        if plan in ("free", "trial"):
            limit_bytes = 5 * 1024 * 1024 * 1024
        elif plan in ("individual", "photographers"):
            limit_bytes = 1024 * 1024 * 1024 * 1024
        elif plan in ("studios", "agencies"):
            unlimited = True

        return {
            "photosUploaded": int(count or 0),
            "storageUsedBytes": int(total_bytes or 0),
            "storageLimitBytes": int(limit_bytes) if (limit_bytes is not None) else None,
            "unlimitedStorage": unlimited,
        }
    except HTTPException:
        raise
    except Exception as ex:
        return JSONResponse({"error": f"Failed to fetch usage: {ex}"}, status_code=500)


@router.post("/backfill")
async def billing_backfill(request: Request, payload: dict = Body(None), db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    target_uid = uid
    try:
        if isinstance(payload, dict) and payload.get("uid"):
            from core.auth import require_admin
            ok, _ = require_admin(request, [])
            if ok:
                target_uid = str(payload.get("uid") or uid)
    except Exception:
        target_uid = uid
    try:
        processed, total = _backfill_user_storage(db, target_uid)
        return {"ok": True, "processed": int(processed or 0), "bytes": int(total or 0)}
    except Exception as ex:
        return JSONResponse({"error": f"Failed to backfill: {ex}"}, status_code=500)

def _is_image_key(key: str) -> bool:
    try:
        ext = os.path.splitext(key)[1].lower()
        return ext in (".jpg",".jpeg",".png",".webp",".heic",".tif",".tiff",".gif",".bin")
    except Exception:
        return False

def _backfill_user_storage(db: Session, uid: str) -> tuple[int, int]:
    processed = 0
    total = 0
    try:
        if s3 and R2_BUCKET:
            try:
                bucket = s3.Bucket(R2_BUCKET)
                for obj in bucket.objects.filter(Prefix=f"users/{uid}/"):
                    k = obj.key
                    if not k or k.endswith("/"):
                        continue
                    if not _is_image_key(k):
                        continue
                    sz = int(getattr(obj, "size", 0) or 0)
                    if sz <= 0:
                        continue
                    rec = db.query(GalleryAsset).filter(GalleryAsset.key == k).first()
                    if rec:
                        rec.user_uid = uid
                        rec.vault = None
                        rec.size_bytes = sz
                    else:
                        db.add(GalleryAsset(user_uid=uid, vault=None, key=k, size_bytes=sz))
                    processed += 1
                    total += sz
                for obj in bucket.objects.filter(Prefix=f"portfolios/{uid}/files/"):
                    k = obj.key
                    if not k or k.endswith("/"):
                        continue
                    if not _is_image_key(k):
                        continue
                    sz = int(getattr(obj, "size", 0) or 0)
                    if sz <= 0:
                        continue
                    rec = db.query(GalleryAsset).filter(GalleryAsset.key == k).first()
                    if rec:
                        rec.user_uid = uid
                        rec.vault = None
                        rec.size_bytes = sz
                    else:
                        db.add(GalleryAsset(user_uid=uid, vault=None, key=k, size_bytes=sz))
                    processed += 1
                    total += sz
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
        else:
            try:
                base_dir = os.path.join(static_dir, "users", uid)
                if os.path.isdir(base_dir):
                    for root, _, files in os.walk(base_dir):
                        for f in files:
                            key = os.path.relpath(os.path.join(root, f), static_dir).replace("\\", "/")
                            if not _is_image_key(key):
                                continue
                            path = os.path.join(static_dir, key)
                            try:
                                sz = int(os.path.getsize(path))
                            except Exception:
                                sz = 0
                            if sz <= 0:
                                continue
                            rec = db.query(GalleryAsset).filter(GalleryAsset.key == key).first()
                            if rec:
                                rec.user_uid = uid
                                rec.vault = None
                                rec.size_bytes = sz
                            else:
                                db.add(GalleryAsset(user_uid=uid, vault=None, key=key, size_bytes=sz))
                            processed += 1
                            total += sz
                pdir = os.path.join(static_dir, "portfolios", uid, "files")
                if os.path.isdir(pdir):
                    for root, _, files in os.walk(pdir):
                        for f in files:
                            key = os.path.relpath(os.path.join(root, f), static_dir).replace("\\", "/")
                            if not _is_image_key(key):
                                continue
                            path = os.path.join(static_dir, key)
                            try:
                                sz = int(os.path.getsize(path))
                            except Exception:
                                sz = 0
                            if sz <= 0:
                                continue
                            rec = db.query(GalleryAsset).filter(GalleryAsset.key == key).first()
                            if rec:
                                rec.user_uid = uid
                                rec.vault = None
                                rec.size_bytes = sz
                            else:
                                db.add(GalleryAsset(user_uid=uid, vault=None, key=key, size_bytes=sz))
                            processed += 1
                            total += sz
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
    except Exception:
        pass
    return processed, total
