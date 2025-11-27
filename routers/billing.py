from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from datetime import datetime

from core.auth import get_uid_from_request
from core.database import get_db
from models.user import User

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

