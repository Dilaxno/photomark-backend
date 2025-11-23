from fastapi import APIRouter, Request, Body, Depends
from fastapi.responses import JSONResponse
from typing import Any, Dict, List, Optional
import os
import time

from core.auth import get_uid_from_request
from core.config import logger
from sqlalchemy.orm import Session
from core.database import get_db
from models.replies import Reply

# Firestore removed in Neon migration

router = APIRouter(prefix="/api/replies", tags=["replies"])  # inbound + list


# Inbound webhook from your email provider
# Expected JSON (example):
# {
#   "from": {"email": "sender@example.com", "name": "Alice"},
#   "to": [{"email": "Marouane@photomark.cloud"}],
#   "subject": "Re: A small tool I built for photographers",
#   "text": "Reply body...",
#   "html": "<p>Reply body</p>",
#   "message_id": "<id@provider>",
#   "in_reply_to": "<original@id>"
# }
@router.post("/inbound")
async def inbound_email(request: Request, payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    provider_token = os.getenv("REPLY_WEBHOOK_TOKEN", "").strip()
    auth = request.headers.get("x-inbound-token", "").strip()
    if provider_token and auth != provider_token:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        data = {
            "from_email": ((payload.get("from") or {}).get("email") or "").strip(),
            "from_name": ((payload.get("from") or {}).get("name") or "").strip(),
            "subject": (payload.get("subject") or "").strip(),
            "text": (payload.get("text") or "").strip(),
            "html": (payload.get("html") or "").strip(),
            "message_id": (payload.get("message_id") or "").strip(),
            "in_reply_to": (payload.get("in_reply_to") or "").strip(),
            "ts": int(time.time()),
        }
        # Basic validation
        if not data["from_email"] or not data["text"]:
            return JSONResponse({"error": "missing from_email or text"}, status_code=400)

        # Store in PostgreSQL; replies are not tied to owner_uid here, set to system
        rec = Reply(
            owner_uid="system",
            target_id=data.get("in_reply_to") or data.get("message_id") or "",
            target_type="email",
            from_email=data["from_email"],
            from_name=data.get("from_name"),
            subject=data.get("subject"),
            text=data.get("text"),
            html=data.get("html"),
            ts=data["ts"],
        )
        db.add(rec)
        db.commit()
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"[replies.inbound] error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("")
async def list_replies(
    request: Request,
    limit: int = 50,
    before_ts: Optional[int] = None,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db)
):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        qlimit = max(1, min(int(limit), 500))
        query = db.query(Reply)
        if start_ts is not None:
            query = query.filter(Reply.ts >= int(start_ts))
        if end_ts is not None:
            query = query.filter(Reply.ts <= int(end_ts))
        if before_ts is not None:
            query = query.filter(Reply.ts < int(before_ts))
        # Order by ts desc for pagination
        rows = query.order_by(Reply.ts.desc()).limit(qlimit).all()
        items: List[Dict[str, Any]] = []
        for r in rows:
            items.append({
                "id": r.id,
                "from": {"email": r.from_email or "", "name": r.from_name or ""},
                "subject": r.subject or "",
                "text": r.text or "",
                "html": r.html or "",
                "ts": int(r.ts or 0),
                "createdAt": r.created_at.isoformat() if r.created_at else None,
            })

        # In-memory substring filter for subject/sender/body (case-insensitive)
        if q:
            ql = q.strip().lower()
            items = [it for it in items if (
                (it.get("subject") or "").lower().find(ql) >= 0 or
                (it.get("from", {}).get("email") or "").lower().find(ql) >= 0 or
                (it.get("from", {}).get("name") or "").lower().find(ql) >= 0 or
                (it.get("text") or "").lower().find(ql) >= 0
            )]

        next_cursor = items[-1]["ts"] if len(items) >= qlimit else None
        return {"ok": True, "items": items, "nextCursor": next_cursor}
    except Exception as ex:
        logger.exception(f"[replies.list] error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)
