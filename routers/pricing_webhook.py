from fastapi import APIRouter, Request, Header, Body, Depends
from fastapi.responses import JSONResponse
from typing import Optional
import os
import json
from datetime import datetime

from core.config import logger
from utils.storage import read_json_key, write_json_key
from standardwebhooks import Webhook, WebhookVerificationError
from core.auth import (
    get_uid_from_request,
    get_uid_by_email,
    firebase_enabled,
    fb_auth,
)
from sqlalchemy.orm import Session
from core.database import get_db, SessionLocal
from models.user import User
from models.pricing import PricingEvent, Subscription, Invoice, PaymentMethod
from models.affiliates import AffiliateProfile, AffiliateAttribution, AffiliateConversion
from models.shop_sales import ShopSale
from models.shop import ShopSlug, Shop
from utils.emailing import send_email_smtp, render_email
import uuid

# Firestore dependency removed in Neon migration

router = APIRouter(prefix="/api/pricing", tags=["pricing"]) 


# Helpers

def _entitlement_key(uid: str) -> str:
    return f"users/{uid}/billing/entitlement.json"


def _normalize_plan(plan: Optional[str]) -> str:
    p = (plan or "").strip().lower()
    if not p:
        return ""
    # Normalize separators and remove common suffixes like "plan"
    p = p.replace("_", " ").replace("-", " ")
    if p.endswith(" plan"):
        p = p[:-5]
    if p.endswith(" plans"):
        p = p[:-6]
    p = p.strip()

    # Match Golden Offer (3-year plan)
    if "golden" in p:
        return "golden"
    # Match new plan names
    if "individual" in p or p in ("ind", "indiv", "solo", "i"):
        return "individual"
    if "studio" in p or p in ("st", "s", "team", "teams"):
        return "studios"
    # Backward compatibility for old plan names
    if "photograph" in p or p in ("photo", "pg", "p", "photographers", "photographer"):
        return "individual"
    if "agenc" in p or p in ("ag", "agencies", "agency"):
        return "studios"
    return ""


def _allowed_plans() -> set[str]:
    # Optionally controlled by env. Defaults are the internal plans
    raw = (os.getenv("PRICING_ALLOWED_PLANS") or os.getenv("ALLOWED_PLANS") or "").strip()
    if raw:
        out: set[str] = set()
        for tok in raw.split(","):
            slug = _normalize_plan(tok)
            if slug:
                out.add(slug)
        if out:
            return out
    return {"individual", "studios", "golden"}


def _first_email_from_payload(payload: dict) -> str:
    candidates = []
    try:
        # Some common paths across providers
        paths = (
            ["email"],
            ["customer", "email"],
            ["data", "object", "email"],
            ["data", "object", "customer_email"],
            ["object", "customer_email"],
            ["object", "email"],
            ["metadata", "email"],
        )
        for path in paths:
            node = payload
            for key in path:
                if isinstance(node, dict) and key in node:
                    node = node[key]
                else:
                    node = None
                    break
            if isinstance(node, str) and "@" in node:
                candidates.append(node.strip().lower())
    except Exception:
        pass
    return candidates[0] if candidates else ""


def _combine_name(first: str, last: str) -> str:
    f = (first or "").strip()
    l = (last or "").strip()
    if f and l:
        return f"{f} {l}"
    return f or l


def _first_customer_name(payload: dict) -> str:
    """
    Try to extract a customer's full name from typical webhook shapes.
    Looks at:
      - customer.name / customer.first_name + customer.last_name
      - billing.name
      - top-level name / customer_name
      - nested data/object wrappers
    """
    try:
        cust = payload.get("customer") if isinstance(payload, dict) else None
        if isinstance(cust, dict):
            nm = str(cust.get("name") or "").strip()
            if nm:
                return nm
            first = str(cust.get("first_name") or cust.get("firstName") or "").strip()
            last = str(cust.get("last_name") or cust.get("lastName") or "").strip()
            nm = _combine_name(first, last)
            if nm:
                return nm
        billing = payload.get("billing") if isinstance(payload, dict) else None
        if isinstance(billing, dict):
            nm = str(billing.get("name") or "").strip()
            if nm:
                return nm
    except Exception:
        pass
    # Deep scan common keys
    for k in ("customer_name", "name", "full_name"):
        nm = _deep_find_first(payload if isinstance(payload, dict) else {}, (k,))
        if isinstance(nm, str) and nm.strip():
            return nm.strip()
    # Try separate first/last via deep scan
    first = _deep_find_first(payload if isinstance(payload, dict) else {}, ("first_name", "firstName"))
    last = _deep_find_first(payload if isinstance(payload, dict) else {}, ("last_name", "lastName"))
    if isinstance(first, str) or isinstance(last, str):
        return _combine_name(str(first or ""), str(last or ""))
    return ""


def _first_city_country(payload: dict) -> tuple[str, str]:
    """
    Extract billing city and country (alpha-2 or full) from typical positions:
      - billing.city / billing.country
      - billing_address.city / billing_address.country
      - address.city / address.country
    Falls back to a deep scan for 'city' and 'country' if needed.
    """
    city = ""
    country = ""
    try:
        billing = payload.get("billing") if isinstance(payload, dict) else None
        if isinstance(billing, dict):
            city = str(billing.get("city") or "").strip() or city
            country = str(billing.get("country") or "").strip() or country
        if not city or not country:
            baddr = payload.get("billing_address") if isinstance(payload, dict) else None
            if isinstance(baddr, dict):
                city = city or str(baddr.get("city") or "").strip()
                country = country or str(baddr.get("country") or "").strip()
        if not city or not country:
            addr = payload.get("address") if isinstance(payload, dict) else None
            if isinstance(addr, dict):
                city = city or str(addr.get("city") or "").strip()
                country = country or str(addr.get("country") or "").strip()
    except Exception:
        pass
    if not city:
        c = _deep_find_first(payload if isinstance(payload, dict) else {}, ("city",))
        city = str(c or "").strip()
    if not country:
        c = _deep_find_first(payload if isinstance(payload, dict) else {}, ("country", "country_code", "countryCode"))
        country = str(c or "").strip()
    # Normalize country to upper alpha-2 when likely a code
    if len(country) in (2, 3):
        try:
            country = country.upper()
        except Exception:
            pass
    return city, country


def _extract_payment_method(obj: dict, full: dict | None = None) -> dict:
    """
    Best-effort extraction of a single payment method used during checkout/subscription events.
    Returns normalized shape:
      { id, type, last4, expiry, isDefault }
    """
    try:
        def _g(keys: tuple[str, ...]) -> str:
            v = _deep_find_first(obj or {}, keys)
            if not v and isinstance(full, dict):
                v = _deep_find_first(full, keys)
            return str(v or "").strip()

        pm_id = _g(("payment_method_id", "paymentMethodId", "pm_id", "payment_method", "paymentMethod"))
        # Some providers return an object for payment_method with an id field
        if not pm_id:
            try:
                pm = (obj.get("payment_method") if isinstance(obj, dict) else None) or (full.get("payment_method") if isinstance(full, dict) else None)
                if isinstance(pm, dict):
                    pm_id = str((pm.get("payment_method_id") or pm.get("id") or "")).strip()
            except Exception:
                pm_id = ""
        # Fallback: sometimes we only have payment_id/id; use it as a stable key
        if not pm_id:
            pm_id = _g(("payment_id", "id"))

        last4 = _g(("last4", "last4_digits", "last_4", "card_last4", "card_last_four", "lastFour", "last_four"))
        brand_raw = _g(("card_network", "card_brand", "brand", "network", "scheme", "card_type", "cardType"))
        typ_raw = _g(("payment_method_type", "paymentMethodType", "type"))

        exp_m = _g(("expiry_month", "exp_month", "card_expiry_month", "expiryMonth"))
        exp_y = _g(("expiry_year", "exp_year", "card_expiry_year", "expiryYear"))

        # Try to extract from nested card object if not found
        if not last4 or not brand_raw:
            try:
                # Check for nested card object in various locations
                card_obj = None
                for src in [obj, full]:
                    if not isinstance(src, dict):
                        continue
                    card_obj = (
                        src.get("card") or 
                        src.get("card_details") or 
                        src.get("payment_method", {}).get("card") if isinstance(src.get("payment_method"), dict) else None
                    )
                    if isinstance(card_obj, dict):
                        break
                    # Check in data.object.card
                    data = src.get("data")
                    if isinstance(data, dict):
                        obj_inner = data.get("object")
                        if isinstance(obj_inner, dict):
                            card_obj = obj_inner.get("card") or obj_inner.get("card_details")
                            if isinstance(card_obj, dict):
                                break
                
                if isinstance(card_obj, dict):
                    if not last4:
                        last4 = str(card_obj.get("last4") or card_obj.get("last_4") or card_obj.get("last4_digits") or "").strip()
                    if not brand_raw:
                        brand_raw = str(card_obj.get("brand") or card_obj.get("network") or card_obj.get("card_brand") or "").strip()
                    if not exp_m:
                        exp_m = str(card_obj.get("exp_month") or card_obj.get("expiry_month") or "").strip()
                    if not exp_y:
                        exp_y = str(card_obj.get("exp_year") or card_obj.get("expiry_year") or "").strip()
            except Exception:
                pass

        expiry = ""
        try:
            if exp_m and exp_y:
                mm = str(exp_m).zfill(2)
                yy = str(exp_y)[-2:]
                expiry = f"{mm}/{yy}"
        except Exception:
            expiry = ""

        t = ""
        b = brand_raw.lower()
        if "visa" in b:
            t = "visa"
        elif "master" in b:
            t = "mastercard"
        elif "amex" in b or "american" in b:
            t = "amex"
        elif "discover" in b:
            t = "discover"
        elif "diners" in b:
            t = "diners"
        elif "jcb" in b:
            t = "jcb"
        elif "unionpay" in b or "union" in b:
            t = "unionpay"
        else:
            t = (typ_raw or "card").lower()

        is_default_raw = _g(("is_default", "default", "isDefault", "recurring_default"))
        is_default = str(is_default_raw).lower() in ("1", "true", "yes")

        # Only return if we have at least an id or last4
        if not (pm_id or last4):
            return {}

        return {
            "id": pm_id or f"{t}-{(last4[-4:] if last4 else 'xxxx')}",
            "type": t,
            "last4": (last4[-4:] if last4 else ""),
            "expiry": expiry,
            "isDefault": is_default,
        }
    except Exception:
        return {}
def _deep_find_first(obj: dict, keys: tuple[str, ...]) -> str:
    """Recursively search a dict for the first non-empty string value for any key in keys.
    Limits depth and size to avoid pathological payloads.
    """
    if not isinstance(obj, dict):
        return ""
    seen: set[int] = set()

    def _walk(node: dict, depth: int) -> str:
        if depth > 6:
            return ""
        node_id = id(node)
        if node_id in seen:
            return ""
        seen.add(node_id)

        # Direct match on this level
        for k in keys:
            if k in node:
                v = node.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        # Check common wrappers
        for k in ("object", "data", "attributes", "details"):
            sub = node.get(k)
            if isinstance(sub, dict):
                got = _walk(sub, depth + 1)
                if got:
                    return got
            elif isinstance(sub, list):
                for it in sub[:50]:
                    if isinstance(it, dict):
                        got = _walk(it, depth + 1)
                        if got:
                            return got
        # Generic recursive descent over other dict and list values
        for v in list(node.values())[:100]:
            if isinstance(v, dict):
                got = _walk(v, depth + 1)
                if got:
                    return got
            elif isinstance(v, list):
                for it in v[:50]:
                    if isinstance(it, dict):
                        got = _walk(it, depth + 1)
                        if got:
                            return got
        return ""

    return _walk(obj, 0)


def _plan_from_products(obj: dict) -> str:
    """Infer plan from Dodo payload products when explicit plan metadata is missing.
    Prefers mapping by configured product IDs, then by product names, and only returns
    one of the allowed internal slugs: 'individual' or 'studios'.
    """
    allowed = _allowed_plans()
    ids_individual: set[str] = set(
        s.strip() for s in (
            os.getenv("DODO_INDIVIDUAL_PRODUCT_ID") or "",
            os.getenv("DODO_PHOTOGRAPHERS_PRODUCT_ID") or "",
            os.getenv("DODO_INDIVIDUAL_MONTHLY_PRODUCT_ID") or "",
            os.getenv("DODO_INDIVIDUAL_YEARLY_PRODUCT_ID") or "",
        )
        if s and s.strip()
    )
    ids_studios: set[str] = set(
        s.strip() for s in (
            os.getenv("DODO_STUDIOS_PRODUCT_ID") or "",
            os.getenv("DODO_AGENCIES_PRODUCT_ID") or "",
            os.getenv("DODO_STUDIOS_MONTHLY_PRODUCT_ID") or "",
            os.getenv("DODO_STUDIOS_YEARLY_PRODUCT_ID") or "",
            os.getenv("DODO_GOLDEN_PRODUCT_ID") or "",
        )
        if s and s.strip()
    )
    # Golden Offer product IDs (3-year plan with Studios features)
    ids_golden: set[str] = set(
        s.strip() for s in (
            os.getenv("DODO_GOLDEN_PRODUCT_ID") or "",
            os.getenv("DODO_GOLDEN_OFFER_PRODUCT_ID") or "",
        )
        if s and s.strip()
    )
    found_studios = False
    found_individual = False
    found_golden = False
    names: list[str] = []

    try:
        # Collect potential arrays where products may be listed
        candidate_lists = []
        for key in ("product_cart", "items", "products", "lines", "line_items"):
            val = obj.get(key)
            if isinstance(val, list) and val:
                candidate_lists.append(val)
            elif isinstance(val, dict):
                # Some providers use objects with a nested 'data' array
                data_arr = val.get("data") if isinstance(val.get("data"), list) else None
                if data_arr:
                    candidate_lists.append(data_arr)

        # Inspect each list entry and try to resolve product id/name
        for items in candidate_lists:
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                pid = str((it.get("product_id") or it.get("price_id") or it.get("id") or "")).strip()
                name = str((it.get("product_name") or it.get("name") or it.get("title") or "")).strip()

                # Nested price/product structures
                p = it.get("product") if isinstance(it.get("product"), dict) else None
                pr = it.get("price") if isinstance(it.get("price"), dict) else None
                if p:
                    pid = pid or str((p.get("id") or p.get("product_id") or "")).strip()
                    name = name or str((p.get("name") or p.get("title") or "")).strip()
                if pr:
                    # Some APIs put product under price.product
                    pp = pr.get("product") if isinstance(pr.get("product"), dict) else None
                    if pp:
                        pid = pid or str((pp.get("id") or pp.get("product_id") or "")).strip()
                        name = name or str((pp.get("name") or pp.get("title") or "")).strip()
                    # Or the price itself is the id we map to a product id
                    pid = pid or str((pr.get("id") or pr.get("price_id") or "")).strip()

                # Compare ids against configured product ids
                if pid and pid in ids_golden:
                    found_golden = True
                elif pid and pid in ids_studios:
                    found_studios = True
                if pid and pid in ids_individual:
                    found_individual = True
                if name:
                    names.append(name)

        # Sometimes a single product object may be present
        if isinstance(obj.get("product"), dict):
            p = obj.get("product") or {}
            pid = str((p.get("id") or p.get("product_id") or "")).strip()
            name = str((p.get("name") or p.get("title") or "")).strip()
            if pid and pid in ids_golden:
                found_golden = True
            elif pid and pid in ids_studios:
                found_studios = True
            if pid and pid in ids_individual:
                found_individual = True
            if name:
                names.append(name)

        # Check for product_id directly at top level (common in subscription updates)
        if not (found_studios or found_individual or found_golden):
            top_pid = str((obj.get("product_id") or "")).strip()
            logger.info(f"[pricing.webhook] top-level product_id check: top_pid={top_pid!r} ids_studios={ids_studios} ids_individual={ids_individual} ids_golden={ids_golden}")
            if top_pid:
                if top_pid in ids_golden:
                    found_golden = True
                    logger.info(f"[pricing.webhook] matched golden via top_pid={top_pid}")
                elif top_pid in ids_studios:
                    found_studios = True
                    logger.info(f"[pricing.webhook] matched studios via top_pid={top_pid}")
                if top_pid in ids_individual:
                    found_individual = True
                    logger.info(f"[pricing.webhook] matched individual via top_pid={top_pid}")

        # Fallback: bounded deep scan for id-like fields if nothing found so far
        if not (found_studios or found_individual or found_golden):
            seen_ids: set[str] = set()
            def _scan_ids(node: dict, depth: int = 0):
                if depth > 4 or not isinstance(node, dict):
                    return
                # Common id fields
                for k in ("product_id", "productId", "price_id", "priceId", "id"):
                    v = node.get(k)
                    if isinstance(v, str) and v.strip():
                        seen_ids.add(v.strip())
                # Nested objects commonly used
                for k in ("product", "price", "data", "object", "item", "attributes"):
                    v = node.get(k)
                    if isinstance(v, dict):
                        _scan_ids(v, depth + 1)
                    elif isinstance(v, list):
                        for it in v[:50]:
                            if isinstance(it, dict):
                                _scan_ids(it, depth + 1)
            _scan_ids(obj)
            if ids_golden and any(x in seen_ids for x in ids_golden):
                found_golden = True
            elif ids_studios and any(x in seen_ids for x in ids_studios):
                found_studios = True
            if ids_individual and any(x in seen_ids for x in ids_individual):
                found_individual = True

        try:
            logger.info(f"[pricing.webhook] product mapping: found_studios={found_studios} found_individual={found_individual} found_golden={found_golden} names={names}")
        except Exception:
            pass
    except Exception:
        pass

    try:
        logger.info(f"[pricing.webhook] product mapping: found_studios={found_studios} found_individual={found_individual} found_golden={found_golden} names={names}")
    except Exception:
        pass

    if found_golden:
        return "golden"
    if found_studios:
        return "studios"
    if found_individual:
        return "individual"

    # Fallback: try names
    for nm in names:
        slug = _normalize_plan(nm)
        if slug in allowed:
            return slug

    # Hint mapping via payment_link / checkout_session identifiers when providers omit product arrays
    try:
        # Collect candidate identifier strings from common fields
        candidates: list[str] = []
        def _collect(node: object, keys: tuple[str, ...] = ("payment_link", "checkout_session_id", "payment_id"), depth: int = 0):
            if depth > 4:
                return
            if isinstance(node, dict):
                for k in keys:
                    v = node.get(k)  # type: ignore[attr-defined]
                    if isinstance(v, str) and v.strip():
                        candidates.append(v.strip())
                    elif isinstance(v, dict):
                        vid = v.get("id") if isinstance(v.get("id"), str) else None
                        if vid:
                            candidates.append(str(vid))
                # Recurse into likely wrappers
                for kk in ("object", "data", "attributes", "details"):
                    vv = node.get(kk)  # type: ignore[attr-defined]
                    if isinstance(vv, (dict, list)):
                        _collect(vv, keys, depth + 1)
            elif isinstance(node, list):
                for it in node[:50]:
                    _collect(it, keys, depth + 1)
        _collect(obj)
        # Env-mapped identifiers for direct matching
        pid_link_phot = (os.getenv("DODO_PHOTOGRAPHERS_PAYMENT_LINK_ID") or "").strip()
        pid_link_ag   = (os.getenv("DODO_AGENCIES_PAYMENT_LINK_ID") or "").strip()
        pid_chk_phot  = (os.getenv("DODO_PHOTOGRAPHERS_CHECKOUT_ID") or "").strip()
        pid_chk_ag    = (os.getenv("DODO_AGENCIES_CHECKOUT_ID") or "").strip()
        for c in candidates:
            if (pid_link_ag and pid_link_ag in c) or (pid_chk_ag and pid_chk_ag in c):
                return "studios"
            if (pid_link_phot and pid_link_phot in c) or (pid_chk_phot and pid_chk_phot in c):
                return "individual"
    except Exception:
        pass

    # Ultimate fallback: deep scan any string field for plan labels
    try:
        def _deep_text_scan(n, depth=0):
            if depth > 6:
                return ""
            if isinstance(n, str):
                s = n.lower()
                if "golden" in s:
                    return "golden"
                if ("agenc" in s) or ("studio" in s):
                    return "studios"
                if ("photograph" in s) or ("individual" in s) or ("solo" in s):
                    return "individual"
                return ""
            if isinstance(n, dict):
                for v in list(n.values())[:100]:
                    got = _deep_text_scan(v, depth + 1)
                    if got:
                        return got
            elif isinstance(n, list):
                for it in n[:100]:
                    got = _deep_text_scan(it, depth + 1)
                    if got:
                        return got
            return ""
        ds = _deep_text_scan(obj)
        if ds and ds in allowed:
            try:
                logger.info(f"[pricing.webhook] deep-scan inferred plan={ds}")
            except Exception:
                pass
            return ds
    except Exception:
        pass
    return ""


@router.get("/user")
async def pricing_user(request: Request, db: Session = Depends(get_db)):
    """Return authenticated user's uid, email and current plan for the pricing page.
    Response: { uid, email, plan, isPaid }
    """
    uid = get_uid_from_request(request)
    if not uid:
        logger.info("[pricing.user] unauthorized request (no uid)")
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    email = ""
    plan = "free"
    is_paid = False

    # Read from PostgreSQL
    try:
        u = db.query(User).filter(User.uid == uid).first()
        if u:
            email = (u.email or "").strip()
            plan = u.plan or plan
    except Exception as ex:
        logger.debug(f"[pricing.user] db read failed for {uid}: {ex}")

    # Fallback to entitlement mirror for isPaid
    try:
        ent = read_json_key(_entitlement_key(uid)) or {}
        if ent:
            is_paid = bool(ent.get("isPaid") or False)
            # If plan not set from DB, use mirror
            if plan == "free":
                plan = str(ent.get("plan") or plan)
    except Exception:
        pass

    # Optional: fetch email from Firebase Auth if not in Firestore
    if not email and firebase_enabled and fb_auth:
        try:
            user = fb_auth.get_user(uid)
            email = (getattr(user, "email", None) or "").strip()
        except Exception:
            email = ""

    logger.info(f"[pricing.user] return: uid={uid} email='{email}' plan='{plan}' isPaid={bool(is_paid)}")
    return {"uid": uid, "email": email, "plan": plan, "isPaid": bool(is_paid)}


@router.post("/webhook")
async def pricing_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Webhook endpoint to receive payment events for pricing upgrades.
    Security:
      - If PRICING_WEBHOOK_SECRET or DODO_PAYMENTS_WEBHOOK_KEY is set and starts with whsec_,
        verify using provider's Standard Webhooks signature headers.
      - Otherwise require X-Pricing-Secret header to equal the configured secret.
    """

    logger.info("[pricing.webhook] received webhook")
    payload = None

    # --- Step 1: Verify secret ---
    try:
        secret_raw = (
            os.getenv("PRICING_WEBHOOK_SECRET")
            or os.getenv("DODO_PAYMENTS_WEBHOOK_KEY")
            or os.getenv("DODO_WEBHOOK_SECRET")
            or ""
        ).strip()

        if secret_raw:
            if secret_raw.startswith("whsec_"):
                raw_body = await request.body()
                headers = {
                    "webhook-id": request.headers.get("webhook-id") or request.headers.get("Webhook-Id") or "",
                    "webhook-timestamp": request.headers.get("webhook-timestamp") or request.headers.get("Webhook-Timestamp") or "",
                    "webhook-signature": request.headers.get("webhook-signature") or request.headers.get("Webhook-Signature") or "",
                }
                payload = Webhook(secret_raw).verify(data=raw_body, headers=headers)
            else:
                secret_provided = request.headers.get("X-Pricing-Secret") or request.headers.get("x-pricing-secret") or ""
                if secret_provided != secret_raw:
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)
    except Exception:
        if secret_raw:
            return JSONResponse({"error": "invalid signature"}, status_code=401)

    # --- Step 2: Parse JSON payload if not already verified ---
    if payload is None:
        try:
            payload = await request.json()
        except Exception as ex:
            logger.warning(f"[pricing.webhook] invalid JSON: {ex}")
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

    # --- Step 3: Event type ---
    evt_type = str((payload.get("type") or payload.get("event") or "")).strip().lower()

    # --- Step 4: Normalize event object ---
    event_obj = None
    data_node = payload.get("data") if isinstance(payload.get("data"), (dict, list)) else None
    datta_node = payload.get("datta") if isinstance(payload.get("datta"), (dict, list)) else None  # provider typo safeguard
    # Common provider shapes: { data: { object: {...} } }
    if isinstance(data_node, dict) and isinstance(data_node.get("object"), dict):
        event_obj = data_node["object"]
    # Some send arrays: { data: [ { object: {...} }, ... ] }
    elif isinstance(data_node, list) and data_node and isinstance(data_node[0], dict) and isinstance(data_node[0].get("object"), dict):
        event_obj = data_node[0]["object"]
    # Fallback: use data node directly when object wrapper is missing
    elif isinstance(data_node, dict):
        event_obj = data_node
    # Provider typo 'datta' variants
    elif isinstance(datta_node, dict) and isinstance(datta_node.get("object"), dict):
        event_obj = datta_node["object"]
    elif isinstance(datta_node, dict):
        event_obj = datta_node
    elif isinstance(payload.get("object"), dict):
        event_obj = payload["object"]
    else:
        event_obj = payload
    event_obj = event_obj if isinstance(event_obj, dict) else {}

    # Ensure db session when called directly without FastAPI DI
    try:
        _is_session = hasattr(db, "query")
    except Exception:
        _is_session = False
    if not _is_session:
        try:
            db = SessionLocal()
        except Exception:
            pass

    # --- Diagnostics: summarize payload structure to debug missing products ---
    try:
        def _summarize_list(lst):
            if not isinstance(lst, list):
                return {"type": type(lst).__name__}
            head = lst[0] if lst else None
            head_keys = list(head.keys())[:10] if isinstance(head, dict) else type(head).__name__
            return {"len": len(lst), "first_type": type(head).__name__ if head is not None else None, "first_keys": head_keys}

        top_keys = list(payload.keys())[:30]
        obj_keys = list(event_obj.keys())[:30]
        pc = event_obj.get("product_cart")
        items = event_obj.get("items")
        products = event_obj.get("products")
        lines = event_obj.get("lines")
        line_items = event_obj.get("line_items")
        logger.info(
            "[pricing.webhook] diag: top_keys=%s obj_keys=%s pc=%s items=%s products=%s lines=%s line_items=%s",
            top_keys,
            obj_keys,
            _summarize_list(pc),
            _summarize_list(items),
            _summarize_list(products),
            _summarize_list(lines if isinstance(lines, list) else (lines.get('data') if isinstance(lines, dict) else [])),
            _summarize_list(line_items),
        )
    except Exception:
        pass

    # --- Step 5: Extract metadata & query_params (overlay checkout) ---
    def _dict(d):
        return d if isinstance(d, dict) else {}
    payload_data = _dict(payload.get("data")) if isinstance(payload, dict) else {}
    payload_datta = _dict(payload.get("datta")) if isinstance(payload, dict) else {}
    meta = (
        _dict((event_obj or {}).get("metadata"))
        or _dict(payload_data.get("metadata"))
        or _dict(payload_datta.get("metadata"))
        or {}
    )
    # Overlay Checkout passes identifiers under data.query_params; accept 'datta' too
    qp = (
        _dict((event_obj or {}).get("query_params"))
        or _dict(payload_data.get("query_params"))
        or _dict(payload_datta.get("query_params"))
        or {}
    )
    logger.info(f"[pricing.webhook] extracted qp={qp} meta={meta}")

    # Deep-scan fallback: locate a dict containing query_params / metadata anywhere in payload
    if not qp:
        try:
            def _find_first_dict_with_key(node: dict, key: str, depth: int = 0) -> Optional[dict]:
                if depth > 6 or not isinstance(node, dict):
                    return None
                if key in node and isinstance(node.get(key), dict):
                    return node.get(key)
                # Search common wrappers
                for k in ("object", "data", "attributes", "details", "datta"):
                    v = node.get(k)
                    if isinstance(v, dict):
                        got = _find_first_dict_with_key(v, key, depth + 1)
                        if got:
                            return got
                    elif isinstance(v, list):
                        for it in v[:50]:
                            if isinstance(it, dict):
                                got = _find_first_dict_with_key(it, key, depth + 1)
                                if got:
                                    return got
                # Generic descent
                for v in list(node.values())[:100]:
                    if isinstance(v, dict):
                        got = _find_first_dict_with_key(v, key, depth + 1)
                        if got:
                            return got
                    elif isinstance(v, list):
                        for it in v[:50]:
                            if isinstance(it, dict):
                                got = _find_first_dict_with_key(it, key, depth + 1)
                                if got:
                                    return got
                return None
            qp_found = _find_first_dict_with_key(payload if isinstance(payload, dict) else {}, "query_params")
            if isinstance(qp_found, dict):
                qp = qp_found
        except Exception:
            pass
    if not meta:
        try:
            meta_found = _find_first_dict_with_key(payload if isinstance(payload, dict) else {}, "metadata")
            if isinstance(meta_found, dict):
                meta = meta_found
        except Exception:
            pass

    # --- Shop sale tracking (Public Shop) ---
    try:
        shop_slug = ""
        owner_uid_ctx = ""
        cart_items_ctx = []
        currency_ctx = ""

        sid = ""
        try:
            sid = (
                _deep_find_first(event_obj or {}, ("checkout_session_id", "session_id", "checkout_id"))
                or _deep_find_first(payload or {}, ("checkout_session_id", "session_id", "checkout_id"))
            )
        except Exception:
            sid = ""

        cache_ctx = {}
        try:
            if sid:
                cache_ctx = read_json_key(f"shops/cache/sessions/{sid}.json") or {}
                logger.info(f"[pricing.webhook] shop sale: loaded cache by session_id={sid}, cache_ctx keys={list(cache_ctx.keys()) if cache_ctx else []}")
            if not cache_ctx:
                link_url = ""
                try:
                    link_url = (
                        str((event_obj.get("checkout_url") or event_obj.get("session_url") or event_obj.get("url") or ""))
                        or str((payload.get("checkout_url") or payload.get("session_url") or payload.get("url") or ""))
                    ).strip()
                except Exception:
                    link_url = ""
                if link_url:
                    try:
                        code = link_url.rsplit("/", 1)[-1]
                        cache_ctx = read_json_key(f"shops/cache/links/{code}.json") or {}
                        logger.info(f"[pricing.webhook] shop sale: loaded cache by link code={code}, cache_ctx keys={list(cache_ctx.keys()) if cache_ctx else []}")
                    except Exception:
                        pass
        except Exception:
            cache_ctx = {}

        # Log metadata and cache for debugging cart_items
        logger.info(f"[pricing.webhook] shop sale: meta keys={list(meta.keys()) if isinstance(meta, dict) else 'not dict'}, cache_ctx keys={list(cache_ctx.keys()) if cache_ctx else []}")
        logger.info(f"[pricing.webhook] shop sale: meta.cart_items={len(meta.get('cart_items', [])) if isinstance(meta, dict) and isinstance(meta.get('cart_items'), list) else 'none'}, cache_ctx.cart_items={len(cache_ctx.get('cart_items', [])) if isinstance(cache_ctx.get('cart_items'), list) else 'none'}")

        if isinstance(meta, dict):
            shop_slug = str((meta.get("shop_slug") or meta.get("slug") or "" or cache_ctx.get("shop_slug") or "")).strip()
            owner_uid_ctx = str((meta.get("owner_uid") or meta.get("uid") or "" or cache_ctx.get("owner_uid") or "")).strip()
            cart_items_ctx = (meta.get("cart_items") or cache_ctx.get("cart_items") or [])
            currency_ctx = str((meta.get("currency") or cache_ctx.get("currency") or "")).strip().upper()
        else:
            shop_slug = str((cache_ctx.get("shop_slug") or "")).strip()
            owner_uid_ctx = str((cache_ctx.get("owner_uid") or "")).strip()
            cart_items_ctx = cache_ctx.get("cart_items") or []
            currency_ctx = str((cache_ctx.get("currency") or "")).strip().upper()

        if not shop_slug and isinstance(qp, dict):
            shop_slug = str((qp.get("shop_slug") or qp.get("shop") or "")).strip()
        if not owner_uid_ctx and isinstance(qp, dict):
            owner_uid_ctx = str((qp.get("owner_uid") or qp.get("uid") or "")).strip()
        
        logger.info(f"[pricing.webhook] shop sale: final cart_items_ctx count={len(cart_items_ctx) if isinstance(cart_items_ctx, list) else 'not list'}")

        # Only process for completed payment events with shop context
        if (evt_type in {"payment.succeeded", "checkout.completed", "payment.completed"}) and (shop_slug or owner_uid_ctx) and (cart_items_ctx or meta.get("cart_total_cents") or cache_ctx.get("cart_total_cents")):
            # Extract payment_id
            def _deep_first_str(node: dict, keys: tuple[str, ...]) -> str:
                return _deep_find_first(node, keys) if isinstance(node, dict) else ""
            payment_id = str((event_obj.get("payment_id") or "")).strip()
            if not payment_id:
                payment_id = _deep_first_str(event_obj, ("payment_id", "paymentId", "id"))

            # Amount in lowest denomination (cents)
            amount_cents = 0
            try:
                raw_amt = (
                    event_obj.get("amount_total")
                    or event_obj.get("total_amount")
                    or event_obj.get("grand_total")
                    or event_obj.get("amount")
                    or 0
                )
                amount_cents = int(raw_amt) if raw_amt else 0
            except Exception:
                amount_cents = 0

            if amount_cents <= 0:
                try:
                    amount_cents = int((meta.get("cart_total_cents") if isinstance(meta, dict) else 0) or (cache_ctx.get("cart_total_cents") if isinstance(cache_ctx, dict) else 0) or 0)
                except Exception:
                    amount_cents = 0

            # Currency
            currency = (event_obj.get("currency") or currency_ctx or "USD")
            try:
                currency = str(currency).upper()
            except Exception:
                currency = "USD"

            # Resolve shop_uid via slug mapping if not present
            shop_uid_ctx = str((meta.get("shop_uid") or "")).strip()
            if not shop_uid_ctx and shop_slug:
                try:
                    slug_row = db.query(ShopSlug).filter(ShopSlug.slug == shop_slug).first()
                    if slug_row:
                        shop_uid_ctx = slug_row.uid
                except Exception:
                    shop_uid_ctx = ""

            # Idempotency check
            if payment_id:
                existing = db.query(ShopSale).filter(ShopSale.payment_id == payment_id).first()
                if existing:
                    return {"ok": True, "shop_sale_recorded": False, "reason": "duplicate_payment_id"}

            sale_id = payment_id or uuid.uuid4().hex
            try:
                items_payload = cart_items_ctx if isinstance(cart_items_ctx, list) else []
                
                # Log warning if items are empty - this helps debug why top selling products shows 0
                if not items_payload:
                    logger.warning(f"[pricing.webhook] shop sale: items_payload is EMPTY! sid={sid}, shop_slug={shop_slug}, meta has cart_items={isinstance(meta, dict) and 'cart_items' in meta}, cache_ctx has cart_items={'cart_items' in cache_ctx if cache_ctx else False}")
                else:
                    logger.info(f"[pricing.webhook] shop sale: items_payload has {len(items_payload)} items, first item id={items_payload[0].get('id') if items_payload else 'none'}")
                
                customer_email = ""
                try:
                    customer_email = (_first_email_from_payload(event_obj or {}) or _first_email_from_payload(payload or {}) or str((meta.get("email") or "")).strip()).lower()
                except Exception:
                    customer_email = ""
                # Enrich customer details (name, city, country) from event
                cust_name = ""
                city = ""
                country = ""
                try:
                    # Prefer event_obj, fallback to entire payload
                    cust_name = _first_customer_name(event_obj or {}) or _first_customer_name(payload or {})
                    cty, ctry = _first_city_country(event_obj or {})
                    if not cty and not ctry:
                        cty, ctry = _first_city_country(payload or {})
                    city = cty or ""
                    country = ctry or ""
                except Exception:
                    pass

                sale = ShopSale(
                    id=sale_id,
                    payment_id=payment_id or None,
                    owner_uid=owner_uid_ctx or (shop_uid_ctx or ""),
                    shop_uid=shop_uid_ctx or None,
                    slug=shop_slug or None,
                    currency=currency or "USD",
                    amount_cents=int(amount_cents or 0),
                    items=items_payload,
                    metadata=meta or {},
                    delivered=True,  # Auto-mark as delivered for digital products
                    customer_email=customer_email or None,
                    customer_name=cust_name or None,
                    customer_city=city or None,
                    customer_country=country or None,
                )
                db.add(sale)
                db.commit()
                
                # Send email notification to shop owner
                try:
                    owner_uid = owner_uid_ctx or shop_uid_ctx
                    if owner_uid:
                        # Get shop owner's email from User table
                        owner = db.query(User).filter(User.uid == owner_uid).first()
                        if owner and owner.email:
                            # Format amount
                            amount_display = f"${(int(amount_cents or 0) / 100):.2f} {(currency or 'USD').upper()}"
                            
                            # Format items for email
                            email_items = []
                            for item in items_payload:
                                item_title = item.get("title", "Unknown Item")
                                item_price = item.get("price", 0)
                                item_price_display = f"${(item_price / 100):.2f}" if isinstance(item_price, (int, float)) else str(item_price)
                                email_items.append({
                                    "title": item_title,
                                    "price": item_price_display
                                })
                            
                            # Get frontend URL for dashboard link
                            frontend_origin = os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud"
                            dashboard_url = f"{frontend_origin}/shop-editor?tab=earnings"
                            
                            # Render email template
                            html = render_email(
                                "sale_notification.html",
                                amount=amount_display,
                                customer_email=customer_email or "Unknown",
                                items=email_items if email_items else None,
                                dashboard_url=dashboard_url
                            )
                            
                            # Send email
                            send_email_smtp(
                                to_addr=owner.email,
                                subject=f"🎉 New Sale: {amount_display}",
                                html=html,
                                from_addr="sales@photomark.cloud",
                                from_name="Photomark Sales"
                            )
                            logger.info(f"[pricing.webhook] Sent sale notification email to {owner.email}")
                except Exception as email_ex:
                    logger.warning(f"[pricing.webhook] Failed to send sale notification email: {email_ex}")
                    # Don't fail the webhook if email fails
                
                # Generate and send commercial license to buyer
                try:
                    if customer_email:
                        from utils.license_generator import generate_license_pdf, generate_license_data, generate_license_number
                        from utils.storage import upload_bytes
                        from datetime import datetime as dt
                        
                        purchase_date = dt.utcnow()
                        
                        # Get shop details
                        shop_obj = None
                        seller_name_lic = ""
                        shop_name_lic = shop_slug or "Shop"
                        try:
                            if shop_uid_ctx:
                                shop_obj = db.query(Shop).filter(Shop.uid == shop_uid_ctx).first()
                            if not shop_obj and owner_uid_ctx:
                                shop_obj = db.query(Shop).filter(Shop.owner_uid == owner_uid_ctx).first()
                            if shop_obj:
                                shop_name_lic = shop_obj.name or shop_slug or "Shop"
                                seller_name_lic = shop_obj.owner_name or ""
                        except Exception:
                            pass
                        
                        # Generate license number
                        license_number = generate_license_number(payment_id or sale_id, "master", purchase_date)
                        
                        # Generate license PDF
                        license_pdf = generate_license_pdf(
                            license_number=license_number,
                            buyer_name=cust_name or "",
                            buyer_email=customer_email,
                            seller_name=seller_name_lic,
                            shop_name=shop_name_lic,
                            items=items_payload,
                            payment_id=payment_id or sale_id,
                            purchase_date=purchase_date,
                            total_amount=float(amount_cents or 0) / 100,
                            currency=currency or "USD",
                        )
                        
                        # Store license PDF in R2 for later download
                        license_key = f"shops/{shop_uid_ctx or owner_uid_ctx}/licenses/{license_number}.pdf"
                        license_url = upload_bytes(license_key, license_pdf, content_type="application/pdf")
                        
                        # Store license metadata
                        license_data = generate_license_data(
                            payment_id=payment_id or sale_id,
                            buyer_name=cust_name or "",
                            buyer_email=customer_email,
                            seller_name=seller_name_lic,
                            shop_name=shop_name_lic,
                            items=items_payload,
                            purchase_date=purchase_date,
                            total_amount=float(amount_cents or 0) / 100,
                            currency=currency or "USD",
                        )
                        license_data["license_pdf_url"] = license_url
                        
                        # Store license metadata JSON
                        from utils.storage import write_json_key
                        license_meta_key = f"shops/{shop_uid_ctx or owner_uid_ctx}/licenses/{license_number}.json"
                        write_json_key(license_meta_key, license_data)
                        
                        # Format items for buyer email
                        buyer_email_items = []
                        for item in items_payload:
                            item_title = item.get("title", "Digital Product")
                            item_price = item.get("price", 0)
                            item_price_display = f"${(item_price / 100):.2f}" if isinstance(item_price, (int, float)) else str(item_price)
                            buyer_email_items.append({
                                "title": item_title,
                                "price": item_price_display
                            })
                        
                        # Render buyer email
                        buyer_html = render_email(
                            "license_delivery.html",
                            shop_name=shop_name_lic,
                            seller_name=seller_name_lic or shop_name_lic,
                            license_number=license_number,
                            amount=amount_display,
                            items=buyer_email_items if buyer_email_items else None,
                            payment_id=payment_id or sale_id,
                            download_url=license_url,
                        )
                        
                        # Send license email to buyer with PDF attachment
                        send_email_smtp(
                            to_addr=customer_email,
                            subject=f"Your Purchase & Commercial License from {shop_name_lic}",
                            html=buyer_html,
                            from_addr="licenses@photomark.cloud",
                            from_name=f"{shop_name_lic} via Photomark",
                            attachments=[{
                                "filename": f"License-{license_number}.pdf",
                                "content": license_pdf,
                                "mime_type": "application/pdf",
                            }]
                        )
                        logger.info(f"[pricing.webhook] Sent license email to buyer {customer_email}, license: {license_number}")
                except Exception as lic_ex:
                    logger.warning(f"[pricing.webhook] Failed to generate/send license: {lic_ex}")
                    # Don't fail the webhook if license generation fails
                
            except Exception as _ex:
                try:
                    if hasattr(db, "rollback"):
                        db.rollback()
                except Exception:
                    pass
                logger.warning(f"[pricing.webhook] shop sale persist failed: {_ex}")
                return {"ok": True, "shop_sale_recorded": False, "reason": "db_write_failed"}

            return {"ok": True, "shop_sale_recorded": True, "payment_id": payment_id, "amount_cents": int(amount_cents or 0), "currency": currency}
    except Exception as _ex:
        logger.warning(f"[pricing.webhook] shop sale tracking error: {_ex}" )

    # --- Step 6: Resolve UID ---
    uid = ""
    # Prefer query_params for overlay integration
    qp_uid_keys = ("user_uid", "userUid", "uid", "userId", "user-id")
    for k in qp_uid_keys:
        v = str((qp.get(k) if isinstance(qp, dict) else "") or "").strip()
        if v:
            uid = v
            break
    # Fallback to metadata if not found in query_params
    uid_keys = ("user_uid", "userUid", "uid", "userId", "user-id")
    if not uid:
        for k in uid_keys:
            v = str((meta.get(k) if isinstance(meta, dict) else "") or "").strip()
            if v:
                uid = v
                break

    # Fallback by reference fields
    if not uid:
        for src in (event_obj, payload):
            if isinstance(src, dict):
                for k in (
                    "client_reference_id",
                    "reference_id",
                    "external_id",
                    "order_id",
                    "user_uid",
                    "uid",
                    "userUid",
                    "userId",
                    "user-id",
                ):
                    v = str((src.get(k) or "")).strip()
                    if v:
                        uid = v
                        break
            if uid:
                break

    # Fallback: provider-specific nesting (deep scan)
    if not uid and isinstance(payload, dict):
        deep_uid = _deep_find_first(
            payload,
            (
                "user_uid",
                "userUid",
                "uid",
                "userId",
                "user-id",
                "client_reference_id",
                "reference_id",
                "external_id",
                "order_id",
            ),
        )
        if deep_uid:
            uid = deep_uid

    # Fallback by email
    if not uid:
        email = _first_email_from_payload(payload) or _first_email_from_payload(event_obj or {})
        if email:
            try:
                resolved = get_uid_by_email(email)
                if resolved:
                    uid = resolved
            except Exception:
                pass

    if not uid:
        try:
            sample = {k: (v if isinstance(v, (str, int)) else type(v).__name__) for k, v in list((event_obj or {}).items())[:20]}
            logger.warning(f"[pricing.webhook] missing uid; keys hint={list(sample.keys())}")
        except Exception:
            pass
        logger.warning("[pricing.webhook] missing metadata.user_uid; cannot upgrade")
        return {"ok": True, "skipped": True, "reason": "missing_metadata_user_uid"}

    # --- Step 7: Resolve plan and billing cycle ---
    # Prefer overlay query_params plan when present
    plan_raw = str((qp.get("plan") if isinstance(qp, dict) else "") or "").strip() or str((meta.get("plan") if isinstance(meta, dict) else "") or "").strip()
    plan = _normalize_plan(plan_raw)
    logger.info(f"[pricing.webhook] plan resolution: qp={qp} meta_plan={meta.get('plan') if isinstance(meta, dict) else None} plan_raw={plan_raw!r} normalized={plan!r}")
    
    # Extract billing cycle from query_params or metadata
    billing_cycle_raw = str((qp.get("billing") if isinstance(qp, dict) else "") or "").strip() or str((meta.get("billing") if isinstance(meta, dict) else "") or "").strip()
    billing_cycle = None
    if billing_cycle_raw:
        bc = billing_cycle_raw.lower().strip()
        if bc in ("monthly", "month", "m"):
            billing_cycle = "monthly"
        elif bc in ("yearly", "annual", "year", "y", "annually"):
            billing_cycle = "yearly"

    # --- Step 7b: Capture and cache any available context for later payment.succeeded ---
    ctx = {"uid": uid, "plan": plan, "email": _first_email_from_payload(payload) or _first_email_from_payload(event_obj or {})}
    customer_id = ""
    try:
        cust = event_obj.get("customer") if isinstance(event_obj, dict) else None
        if isinstance(cust, dict):
            customer_id = str((cust.get("customer_id") or cust.get("id") or "")).strip()
    except Exception:
        pass
    sub_id = _deep_find_first(event_obj, ("subscription_id", "subscriptionId", "sub_id")) if isinstance(event_obj, dict) else ""
    # Write lightweight cache entries when we have any meaningful context
    try:
        def _write_ctx(key: str):
            if not key:
                return
            write_json_key(f"pricing/cache/{key}.json", {
                "uid": ctx.get("uid") or None,
                "plan": ctx.get("plan") or None,
                "email": ctx.get("email") or None,
                "updatedAt": int(datetime.utcnow().timestamp()),
            })
        if ctx.get("uid") or ctx.get("plan") or ctx.get("email"):
            if sub_id:
                _write_ctx(f"subscriptions/{sub_id}")
            if customer_id:
                _write_ctx(f"customers/{customer_id}")
            if ctx.get("email"):
                _write_ctx(f"emails/{(ctx['email'] or '').lower()}")
    except Exception:
        pass

    # Process upgrades for 'payment.succeeded' and 'subscription.active' (Dodo)
    # Note: For subscriptions, payment.succeeded often lacks product_id, but subscription.active has it
    process_events = {"payment.succeeded", "subscription.active"}
    logger.info(f"[pricing.webhook] processing event: evt_type={evt_type!r} in_process_events={evt_type in process_events}")
    if evt_type not in process_events:
        return {"ok": True, "captured": bool(ctx.get("uid") or ctx.get("plan") or ctx.get("email")), "event_type": evt_type}
    
    # For payment.succeeded with subscription_id but no product info, defer to subscription.active
    sub_id_early = _deep_find_first(event_obj, ("subscription_id", "subscriptionId", "sub_id")) if isinstance(event_obj, dict) else ""
    if evt_type == "payment.succeeded" and sub_id_early:
        # Check if we have product info
        has_product_info = bool(
            event_obj.get("product_id") or
            event_obj.get("product_cart") or
            _deep_find_first(event_obj, ("product_id",))
        )
        if not has_product_info:
            # Persist payment method even if we defer plan resolution to subscription.active
            try:
                if uid:
                    user = db.query(User).filter(User.uid == uid).first()
                    if user:
                        meta_pm = user.extra_metadata or {}
                        pm = _extract_payment_method(event_obj or {}, payload if isinstance(payload, dict) else None)
                        if isinstance(pm, dict) and (pm.get("id") or pm.get("last4")):
                            pm_list = list(meta_pm.get("paymentMethods") or [])
                            updated = False
                            for i, ex in enumerate(pm_list):
                                try:
                                    same_id = (ex.get("id") and pm.get("id") and str(ex.get("id")) == str(pm.get("id")))
                                    same_fingerprint = (str(ex.get("type") or "") == str(pm.get("type") or "")) and (str(ex.get("last4") or "") == str(pm.get("last4") or ""))
                                    if same_id or same_fingerprint:
                                        keep_default = ex.get("isDefault") and not pm.get("isDefault")
                                        merged = {**ex, **{k: v for k, v in pm.items() if v not in (None, "")}}
                                        if keep_default:
                                            merged["isDefault"] = True
                                        pm_list[i] = merged
                                        updated = True
                                        break
                                except Exception:
                                    continue
                            if not updated:
                                any_default = any(bool(x.get("isDefault")) for x in pm_list)
                                if not any_default:
                                    pm["isDefault"] = True
                                pm_list.append(pm)
                            # Ensure only one default
                            try:
                                defaults = [i for i, x in enumerate(pm_list) if bool(x.get("isDefault"))]
                                if len(defaults) > 1:
                                    keep = defaults[-1]
                                    for j, x in enumerate(pm_list):
                                        x["isDefault"] = (j == keep)
                            except Exception:
                                pass
                            meta_pm["paymentMethods"] = pm_list
                            meta_pm["paymentMethod"] = {
                                "brand": pm.get("type"),
                                "last4": pm.get("last4"),
                                "expiry": pm.get("expiry"),
                                "isDefault": bool(pm.get("isDefault")),
                            }
                            user.extra_metadata = meta_pm
                            db.commit()
            except Exception as _pm_ex:
                try:
                    if hasattr(db, "rollback"):
                        db.rollback()
                except Exception:
                    pass
                logger.debug(f"[pricing.webhook] could not persist payment method on payment.succeeded defer: {_pm_ex}")
            logger.info(f"[pricing.webhook] payment.succeeded for subscription {sub_id_early} lacks product info, deferring to subscription.active")
            return {"ok": True, "deferred": True, "reason": "subscription_payment_deferred_to_active", "subscription_id": sub_id_early}

    # Detect subscription-style payloads which may not include product_cart
    sub_id = _deep_find_first(event_obj, ("subscription_id", "subscriptionId", "sub_id")) if isinstance(event_obj, dict) else ""
    is_subscription = bool(sub_id and not (isinstance(event_obj.get("product_cart"), list) and event_obj.get("product_cart")))

    # Optional: gate subscription plan upgrades by status (default allow only 'active')
    try:
        status = str((event_obj.get("status") or "")).strip().lower()
        allowed_raw = str(os.getenv("PRICING_SUBSCRIPTION_ACTIVE_STATUSES") or "active,succeeded").strip()
        allowed_statuses = set([s.strip().lower() for s in allowed_raw.split(",") if s.strip()])
        if is_subscription and status and allowed_statuses and status not in allowed_statuses:
            try:
                logger.info(f"[pricing.webhook] subscription status not active: subscription_id={sub_id} status={status} allowed={sorted(list(allowed_statuses))}")
            except Exception:
                pass
            return {"ok": True, "skipped": True, "reason": "subscription_status_not_active", "status": status}
    except Exception:
        pass

    # If uid/plan missing, try reading cached context by subscription/customer/email
    if (not uid or not plan):
        try:
            def _read_ctx(key: str) -> dict:
                try:
                    return read_json_key(f"pricing/cache/{key}.json") or {}
                except Exception:
                    return {}
            if sub_id and (not uid or not plan):
                c1 = _read_ctx(f"subscriptions/{sub_id}")
                uid = uid or str(c1.get("uid") or "").strip()
                plan = plan or _normalize_plan(str(c1.get("plan") or ""))
            if (not uid or not plan) and customer_id:
                c2 = _read_ctx(f"customers/{customer_id}")
                uid = uid or str(c2.get("uid") or "").strip()
                plan = plan or _normalize_plan(str(c2.get("plan") or ""))
            if (not uid or not plan) and ctx.get("email"):
                c3 = _read_ctx(f"emails/{(ctx.get('email') or '').lower()}")
                uid = uid or str(c3.get("uid") or "").strip()
                plan = plan or _normalize_plan(str(c3.get("plan") or ""))
        except Exception:
            pass

    if not plan and is_subscription:
        # Direct mapping by product_id when present on subscription payload
        try:
            product_id = str((event_obj.get("product_id") or "")).strip()
            if not product_id:
                product_id = _deep_find_first(event_obj, ("product_id", "productId"))
            # Also check subscription object if present (common in payment.succeeded for subscriptions)
            if not product_id:
                sub_obj = event_obj.get("subscription") if isinstance(event_obj.get("subscription"), dict) else None
                if sub_obj:
                    product_id = str((sub_obj.get("product_id") or "")).strip()
                    logger.info(f"[pricing.webhook] found product_id in subscription object: {product_id}")
            # Check full payload as last resort
            if not product_id and isinstance(payload, dict):
                product_id = _deep_find_first(payload, ("product_id", "productId"))
                if product_id:
                    logger.info(f"[pricing.webhook] found product_id in full payload: {product_id}")
            ids_individual: set[str] = set(
                s.strip() for s in (
                    os.getenv("DODO_INDIVIDUAL_PRODUCT_ID") or "",
                    os.getenv("DODO_PHOTOGRAPHERS_PRODUCT_ID") or "",
                    os.getenv("DODO_INDIVIDUAL_MONTHLY_PRODUCT_ID") or "",
                    os.getenv("DODO_INDIVIDUAL_YEARLY_PRODUCT_ID") or "",
                ) if s and s.strip()
            )
            ids_studios: set[str] = set(
                s.strip() for s in (
                    os.getenv("DODO_STUDIOS_PRODUCT_ID") or "",
                    os.getenv("DODO_AGENCIES_PRODUCT_ID") or "",
                    os.getenv("DODO_STUDIOS_MONTHLY_PRODUCT_ID") or "",
                    os.getenv("DODO_STUDIOS_YEARLY_PRODUCT_ID") or "",
                ) if s and s.strip()
            )
            ids_golden: set[str] = set(
                s.strip() for s in (
                    os.getenv("DODO_GOLDEN_PRODUCT_ID") or "",
                    os.getenv("DODO_GOLDEN_OFFER_PRODUCT_ID") or "",
                ) if s and s.strip()
            )
            logger.info(f"[pricing.webhook] direct product_id check: product_id={product_id!r} ids_studios={ids_studios} ids_individual={ids_individual} ids_golden={ids_golden}")
            if product_id:
                if ids_golden and product_id in ids_golden:
                    plan = "golden"
                    logger.info(f"[pricing.webhook] matched golden via direct product_id={product_id}")
                elif ids_studios and product_id in ids_studios:
                    plan = "studios"
                    logger.info(f"[pricing.webhook] matched studios via direct product_id={product_id}")
                elif ids_individual and product_id in ids_individual:
                    plan = "individual"
                    logger.info(f"[pricing.webhook] matched individual via direct product_id={product_id}")
        except Exception:
            pass

        # Optional JSON mapping of subscription_id -> plan via env DODO_SUBSCRIPTION_PLAN_MAP
        if not plan:
            try:
                sid_map_raw = (os.getenv("DODO_SUBSCRIPTION_PLAN_MAP") or "").strip()
                if sid_map_raw:
                    m = {}
                    try:
                        m = json.loads(sid_map_raw)
                    except Exception:
                        m = {}
                    if isinstance(m, dict) and sub_id:
                        v = str(m.get(sub_id) or "").strip()
                        nv = _normalize_plan(v)
                        if nv in _allowed_plans():
                            plan = nv
            except Exception:
                pass

        # Try mapping subscription_id to plan via env (only if plan not already resolved)
        sid = sub_id.strip()
        if not plan:
            sid_phot_old = (os.getenv("DODO_PHOTOGRAPHERS_SUBSCRIPTION_ID") or "").strip()
            sid_ag_old = (os.getenv("DODO_AGENCIES_SUBSCRIPTION_ID") or "").strip()
            sid_individual = (os.getenv("DODO_INDIVIDUAL_SUBSCRIPTION_ID") or "").strip()
            sid_studios = (os.getenv("DODO_STUDIOS_SUBSCRIPTION_ID") or "").strip()
            if sid and (sid_studios and sid == sid_studios or sid_ag_old and sid == sid_ag_old):
                plan = "studios"
            elif sid and (sid_individual and sid == sid_individual or sid_phot_old and sid == sid_phot_old):
                plan = "individual"
        
        # Fallback: try metadata/query params, then product mapping
        if not plan:
            plan = _normalize_plan(plan_raw)
        if not plan:
            plan = _plan_from_products(event_obj or {})
        # Last resort: map against full payload (some providers omit products under object)
        if not plan and isinstance(payload, dict):
            plan = _plan_from_products(payload)
        try:
            logger.info(f"[pricing.webhook] subscription detected: subscription_id={sid} resolved plan={plan or 'UNKNOWN'}")
        except Exception:
            pass

    if not plan and not is_subscription:
        plan = _plan_from_products(event_obj or {})
        if not plan and isinstance(payload, dict):
            plan = _plan_from_products(payload)
    if not plan or plan not in _allowed_plans():
        allowed = sorted(list(_allowed_plans()))
        return {
            "ok": True,
            "skipped": True,
            "reason": "unsupported_plan",
            "plan_raw": plan_raw,
            "normalized": plan,
            "allowed": allowed,
        }

    # Cache the resolved plan against subscription/customer/email for future events that omit product details
    try:
        if sub_id:
            write_json_key(f"pricing/cache/subscriptions/{sub_id}.json", {"uid": uid, "plan": plan, "email": ctx.get("email")})
        if customer_id:
            write_json_key(f"pricing/cache/customers/{customer_id}.json", {"uid": uid, "plan": plan, "email": ctx.get("email")})
        if ctx.get("email"):
            write_json_key(f"pricing/cache/emails/{(ctx.get('email') or '').lower()}", {"uid": uid, "plan": plan})
    except Exception:
        pass

    # --- Step 8: Persist plan to Neon (PostgreSQL) ---
    try:
        user = db.query(User).filter(User.uid == uid).first()
        if not user:
            return {"ok": True, "skipped": True, "reason": "user_not_found"}
        now = datetime.utcnow()
        user.plan = plan
        try:
            status = str((event_obj.get("status") or "")).strip().lower()
        except Exception:
            status = ""
        if sub_id:
            user.subscription_id = sub_id
        if status:
            user.subscription_status = status
        user.updated_at = now
        meta = user.extra_metadata or {}
        # Persist payment method used during checkout/subscription
        try:
            pm = _extract_payment_method(event_obj or {}, payload if isinstance(payload, dict) else None)
        except Exception:
            pm = {}
        if isinstance(pm, dict) and (pm.get("id") or pm.get("last4")):
            pm_list = list(meta.get("paymentMethods") or [])
            updated = False
            for i, ex in enumerate(pm_list):
                try:
                    same_id = (ex.get("id") and pm.get("id") and str(ex.get("id")) == str(pm.get("id")))
                    same_fingerprint = (str(ex.get("type") or "") == str(pm.get("type") or "")) and (str(ex.get("last4") or "") == str(pm.get("last4") or ""))
                    if same_id or same_fingerprint:
                        # Merge while preserving previous default unless pm explicitly sets it
                        keep_default = ex.get("isDefault") and not pm.get("isDefault")
                        merged = {**ex, **{k: v for k, v in pm.items() if v not in (None, "")}}
                        if keep_default:
                            merged["isDefault"] = True
                        pm_list[i] = merged
                        updated = True
                        break
                except Exception:
                    continue
            if not updated:
                any_default = any(bool(x.get("isDefault")) for x in pm_list)
                if not any_default:
                    pm["isDefault"] = True
                pm_list.append(pm)
            # Ensure only one default
            try:
                defaults = [i for i, x in enumerate(pm_list) if bool(x.get("isDefault"))]
                if len(defaults) > 1:
                    keep = defaults[-1]
                    for j, x in enumerate(pm_list):
                        x["isDefault"] = (j == keep)
            except Exception:
                pass
            meta["paymentMethods"] = pm_list
            # Backward compatible single-method summary
            meta["paymentMethod"] = {
                "brand": pm.get("type"),
                "last4": pm.get("last4"),
                "expiry": pm.get("expiry"),
                "isDefault": bool(pm.get("isDefault")),
            }
            
            # Save payment method to Neon PostgreSQL database
            try:
                pm_id = str(pm.get("id") or "").strip()
                pm_type = str(pm.get("type") or "card").strip().lower()
                pm_last4 = str(pm.get("last4") or "").strip()[-4:] if pm.get("last4") else None
                pm_expiry = str(pm.get("expiry") or "").strip() if pm.get("expiry") else None
                pm_is_default = 1 if pm.get("isDefault") else 0
                
                # Parse expiry into month/year
                pm_exp_month = None
                pm_exp_year = None
                if pm_expiry and "/" in pm_expiry:
                    parts = pm_expiry.split("/")
                    if len(parts) == 2:
                        pm_exp_month = parts[0].strip()
                        pm_exp_year = parts[1].strip()
                
                # Check if payment method already exists
                existing_pm = None
                if pm_id:
                    existing_pm = db.query(PaymentMethod).filter(
                        PaymentMethod.user_uid == uid,
                        PaymentMethod.payment_method_id == pm_id
                    ).first()
                
                if not existing_pm and pm_last4:
                    # Try to find by fingerprint (type + last4)
                    existing_pm = db.query(PaymentMethod).filter(
                        PaymentMethod.user_uid == uid,
                        PaymentMethod.type == pm_type,
                        PaymentMethod.last4 == pm_last4
                    ).first()
                
                if existing_pm:
                    # Update existing payment method
                    if pm_expiry:
                        existing_pm.expiry = pm_expiry
                        existing_pm.expiry_month = pm_exp_month
                        existing_pm.expiry_year = pm_exp_year
                    existing_pm.is_default = pm_is_default
                    existing_pm.updated_at = datetime.utcnow()
                else:
                    # Create new payment method
                    new_pm = PaymentMethod(
                        payment_method_id=pm_id or f"{pm_type}-{pm_last4 or 'xxxx'}",
                        user_uid=uid,
                        type=pm_type,
                        last4=pm_last4,
                        expiry=pm_expiry,
                        expiry_month=pm_exp_month,
                        expiry_year=pm_exp_year,
                        brand=pm_type.title() if pm_type else None,
                        is_default=pm_is_default,
                    )
                    db.add(new_pm)
                
                # If this is default, unset other defaults
                if pm_is_default:
                    db.query(PaymentMethod).filter(
                        PaymentMethod.user_uid == uid,
                        PaymentMethod.payment_method_id != (pm_id or f"{pm_type}-{pm_last4 or 'xxxx'}")
                    ).update({"is_default": 0})
                
                logger.info(f"[pricing.webhook] saved payment method to Neon DB for user {uid}")
            except Exception as pm_db_ex:
                logger.warning(f"[pricing.webhook] failed to save payment method to Neon DB: {pm_db_ex}")
                # Don't fail the main operation

        meta.update({
            "isPaid": True,
            "paidAt": now.isoformat(),
            "lastPaymentProvider": "dodo",
            "billingCycle": billing_cycle or None,
        })
        user.extra_metadata = meta
        db.commit()
    except Exception as ex:
        try:
            if hasattr(db, "rollback"):
                db.rollback()
        except Exception:
            pass
        logger.warning(f"[pricing.webhook] failed to persist plan for {uid}: {ex}")
        return {"ok": True, "skipped": True, "reason": "db_write_failed"}

    # --- Step 9: Affiliate commission tracking in PostgreSQL ---
    try:
        # Extract payment details from webhook payload
        amount_cents = 0
        currency = "USD"
        try:
            amount_raw = (
                event_obj.get("amount") or event_obj.get("total") or event_obj.get("amount_total") or event_obj.get("grand_total") or 0
            )
            amount_cents = int(amount_raw) if amount_raw else 0
            currency_raw = (event_obj.get("currency") or event_obj.get("currency_code") or "USD")
            currency = str(currency_raw).upper()
        except Exception:
            pass

        # Resolve affiliate uid via attribution table - check DB first, then JSON fallback
        aff = db.query(AffiliateAttribution).filter(AffiliateAttribution.user_uid == uid).first()
        affiliate_uid = aff.affiliate_uid if aff else None
        
        # Fallback to JSON attribution if DB record doesn't exist
        if not affiliate_uid:
            try:
                attrib_json = read_json_key(f"affiliates/attributions/{uid}.json") or {}
                affiliate_uid = attrib_json.get('affiliate_uid')
                if affiliate_uid:
                    logger.info(f"[pricing.webhook] found affiliate attribution in JSON for user={uid} affiliate={affiliate_uid}")
            except Exception:
                pass
        
        if affiliate_uid and amount_cents > 0:
            # Check if conversion already exists
            existing_conv = db.query(AffiliateConversion).filter(
                AffiliateConversion.affiliate_uid == affiliate_uid,
                AffiliateConversion.user_uid == uid
            ).first()
            
            if existing_conv:
                logger.info(f"[pricing.webhook] conversion already exists for user={uid} affiliate={affiliate_uid}")
            else:
                # Golden/lifetime plan gets 40% commission, others get 30%
                plan_lower = str(plan or '').lower()
                if plan_lower in ('golden', 'golden_offer'):
                    commission_rate = 0.40
                else:
                    commission_rate = float(os.getenv("AFFILIATE_COMMISSION_RATE", "0.30"))
                commission_cents = int(amount_cents * commission_rate)

                # Store in JSON first (no FK constraints)
                try:
                    conv_key = f"affiliates/conversions/{uid}.json"
                    write_json_key(conv_key, {
                        "affiliate_uid": affiliate_uid,
                        "user_uid": uid,
                        "amount_cents": amount_cents,
                        "payout_cents": commission_cents,
                        "currency": currency.lower(),
                        "plan": plan,
                        "converted_at": datetime.utcnow().isoformat(),
                    })
                except Exception as json_ex:
                    logger.warning(f"[pricing.webhook] JSON conversion write failed: {json_ex}")

                # Record conversion in DB
                try:
                    db.add(AffiliateConversion(
                        affiliate_uid=affiliate_uid,
                        user_uid=uid,
                        amount_cents=amount_cents,
                        payout_cents=commission_cents,
                        currency=currency.lower(),
                    ))

                    # Update profile aggregates
                    prof = db.query(AffiliateProfile).filter(AffiliateProfile.uid == affiliate_uid).first()
                    if prof:
                        prof.conversions_total = int(prof.conversions_total or 0) + 1
                        prof.gross_cents_total = int(prof.gross_cents_total or 0) + amount_cents
                        prof.payout_cents_total = int(prof.payout_cents_total or 0) + commission_cents
                        prof.last_conversion_at = datetime.utcnow()
                    db.commit()
                    logger.info(f"[pricing.webhook] recorded conversion for user={uid} affiliate={affiliate_uid} amount={amount_cents} commission={commission_cents}")
                except Exception as db_ex:
                    db.rollback()
                    logger.warning(f"[pricing.webhook] DB conversion insert failed: {db_ex}")
                
                # Update JSON stats mirror
                try:
                    stats = read_json_key(f"affiliates/{affiliate_uid}/stats.json") or {}
                    stats['conversions'] = int(stats.get('conversions') or 0) + 1
                    stats['gross_cents'] = int(stats.get('gross_cents') or 0) + amount_cents
                    stats['payout_cents'] = int(stats.get('payout_cents') or 0) + commission_cents
                    stats['last_conversion_at'] = datetime.utcnow().isoformat()
                    write_json_key(f"affiliates/{affiliate_uid}/stats.json", stats)
                except Exception as stats_ex:
                    logger.warning(f"[pricing.webhook] stats update failed: {stats_ex}")
    except Exception as e:
        logger.warning(f"[pricing.webhook] affiliate tracking failed: {e}")

    # --- Step 10: Local entitlement mirror ---
    try:
        write_json_key(
            _entitlement_key(uid),
            {
                "isPaid": True,
                "plan": plan,
                "updatedAt": event_obj.get("created_at")
                    or event_obj.get("paid_at")
                    or datetime.utcnow().isoformat(),
            },
        )
    except Exception:
        pass

    # --- Step 11: Save invoice to Firestore for user billing history ---
    try:
        # Extract invoice/payment details from webhook
        payment_id = _deep_find_first(event_obj, ("payment_id", "paymentId", "id")) if isinstance(event_obj, dict) else ""
        if not payment_id and isinstance(payload, dict):
            payment_id = _deep_find_first(payload, ("payment_id", "paymentId", "id"))
        
        # Get amount - try multiple extraction methods
        amount_cents = 0
        try:
            # Try direct fields first
            amount_raw = (
                event_obj.get("amount") or event_obj.get("total") or 
                event_obj.get("amount_total") or event_obj.get("grand_total") or 0
            )
            amount_cents = int(amount_raw) if amount_raw else 0
            
            # If still 0, try deep search
            if amount_cents == 0:
                deep_amount = _deep_find_first(event_obj, ("amount", "total", "amount_total", "grand_total", "subtotal"))
                if deep_amount:
                    try:
                        amount_cents = int(deep_amount)
                    except (ValueError, TypeError):
                        pass
            
            # If still 0, try payload root
            if amount_cents == 0 and isinstance(payload, dict):
                amount_raw = (
                    payload.get("amount") or payload.get("total") or 
                    payload.get("amount_total") or payload.get("grand_total") or 0
                )
                if amount_raw:
                    try:
                        amount_cents = int(amount_raw)
                    except (ValueError, TypeError):
                        pass
            
            # If still 0, try to get from subscription or product_cart
            if amount_cents == 0:
                try:
                    # Check subscription object
                    sub_obj = event_obj.get("subscription") if isinstance(event_obj.get("subscription"), dict) else None
                    if sub_obj:
                        sub_amount = sub_obj.get("amount") or sub_obj.get("price") or sub_obj.get("unit_amount")
                        if sub_amount:
                            amount_cents = int(sub_amount)
                    
                    # Check product_cart
                    if amount_cents == 0:
                        cart = event_obj.get("product_cart") or []
                        if isinstance(cart, list):
                            for item in cart:
                                if isinstance(item, dict):
                                    item_price = item.get("price") or item.get("amount") or item.get("unit_amount") or 0
                                    item_qty = item.get("quantity") or 1
                                    try:
                                        amount_cents += int(item_price) * int(item_qty)
                                    except (ValueError, TypeError):
                                        pass
                except Exception:
                    pass
            
            # Fallback: use plan-based pricing if amount is still 0
            if amount_cents == 0:
                plan_prices = {
                    "individual": {"monthly": 2500, "yearly": 24000},  # $25/mo or $240/yr
                    "studios": {"monthly": 4500, "yearly": 43200},     # $45/mo or $432/yr
                    "golden": {"onetime": 19900},                       # $199 lifetime (Golden Offer)
                }
                if plan in plan_prices:
                    if plan == "golden":
                        amount_cents = plan_prices["golden"]["onetime"]
                    elif billing_cycle == "monthly":
                        amount_cents = plan_prices.get(plan, {}).get("monthly", 0)
                    else:
                        amount_cents = plan_prices.get(plan, {}).get("yearly", 0)
                logger.info(f"[pricing.webhook] using fallback price for plan={plan} billing_cycle={billing_cycle}: {amount_cents} cents")
        except Exception as amt_ex:
            logger.warning(f"[pricing.webhook] amount extraction error: {amt_ex}")
        
        # Get currency
        currency = "USD"
        try:
            currency_raw = event_obj.get("currency") or event_obj.get("currency_code") or "USD"
            currency = str(currency_raw).upper()
        except Exception:
            pass
        
        # Get invoice URL if provided by Dodo
        invoice_url = ""
        try:
            invoice_url = _deep_find_first(event_obj, ("invoice_url", "invoiceUrl", "receipt_url", "receiptUrl")) if isinstance(event_obj, dict) else ""
            if not invoice_url and isinstance(payload, dict):
                invoice_url = _deep_find_first(payload, ("invoice_url", "invoiceUrl", "receipt_url", "receiptUrl"))
        except Exception:
            pass
        
        # Get payment date
        payment_date = datetime.utcnow().isoformat().split("T")[0]
        try:
            created_at = event_obj.get("created_at") or event_obj.get("paid_at") or payload.get("created_at")
            if created_at:
                if isinstance(created_at, str):
                    payment_date = created_at.split("T")[0]
                elif isinstance(created_at, (int, float)):
                    payment_date = datetime.utcfromtimestamp(created_at).isoformat().split("T")[0]
        except Exception:
            pass
        
        # Generate invoice ID
        invoice_id = payment_id or f"INV-{uuid.uuid4().hex[:8].upper()}"
        
        # Plan display name
        plan_display = {
            "individual": "Individual Plan",
            "studios": "Studios Plan", 
            "golden": "Golden Lifetime Plan"
        }.get(plan, plan.title() + " Plan")
        
        # Billing cycle display
        billing_display = ""
        if billing_cycle:
            billing_display = f" ({billing_cycle.title()})"
        
        # Save invoice to Neon PostgreSQL database
        try:
            # Check for duplicate invoice
            existing_invoice = db.query(Invoice).filter(Invoice.invoice_id == invoice_id).first()
            if not existing_invoice:
                # Parse invoice date
                invoice_datetime = datetime.utcnow()
                try:
                    if payment_date:
                        invoice_datetime = datetime.strptime(payment_date, "%Y-%m-%d")
                except Exception:
                    pass
                
                new_invoice = Invoice(
                    invoice_id=invoice_id,
                    user_uid=uid,
                    payment_id=payment_id or None,
                    subscription_id=sub_id or None,
                    amount=round(amount_cents / 100, 2) if amount_cents else 0,
                    currency=currency,
                    status="paid",
                    plan=plan,
                    plan_display=plan_display + billing_display,
                    billing_cycle=billing_cycle or None,
                    download_url=invoice_url or None,
                    invoice_date=invoice_datetime,
                )
                db.add(new_invoice)
                db.commit()
                logger.info(f"[pricing.webhook] saved invoice {invoice_id} to Neon DB for user {uid}")
            else:
                logger.info(f"[pricing.webhook] invoice {invoice_id} already exists in Neon DB")
        except Exception as neon_ex:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning(f"[pricing.webhook] failed to save invoice to Neon DB: {neon_ex}")
        
        # Also save invoice to Firestore as backup
        if firebase_enabled:
            from firebase_admin import firestore as fb_firestore
            try:
                fdb = fb_firestore.client()
                invoice_ref = fdb.collection("users").document(uid).collection("invoices").document(invoice_id)
                invoice_ref.set({
                    "id": invoice_id,
                    "date": payment_date,
                    "amount": amount_cents / 100 if amount_cents else 0,
                    "currency": currency,
                    "status": "paid",
                    "plan": plan,
                    "planDisplay": plan_display + billing_display,
                    "billingCycle": billing_cycle or None,
                    "downloadUrl": invoice_url or None,
                    "paymentId": payment_id or None,
                    "subscriptionId": sub_id or None,
                    "createdAt": datetime.utcnow(),
                })
                logger.info(f"[pricing.webhook] saved invoice {invoice_id} to Firestore for user {uid}")
            except Exception as inv_ex:
                logger.warning(f"[pricing.webhook] failed to save invoice to Firestore: {inv_ex}")
        
        # Also save to R2 storage as backup
        try:
            invoices_key = f"users/{uid}/billing/invoices.json"
            existing = read_json_key(invoices_key) or []
            if not isinstance(existing, list):
                existing = []
            
            # Check for duplicate
            if not any(inv.get("id") == invoice_id for inv in existing):
                existing.append({
                    "id": invoice_id,
                    "date": payment_date,
                    "amount": amount_cents / 100 if amount_cents else 0,
                    "currency": currency,
                    "status": "paid",
                    "plan": plan,
                    "planDisplay": plan_display + billing_display,
                    "billingCycle": billing_cycle or None,
                    "downloadUrl": invoice_url or None,
                    "paymentId": payment_id or None,
                    "subscriptionId": sub_id or None,
                })
                # Keep last 100 invoices
                existing = existing[-100:]
                write_json_key(invoices_key, existing)
        except Exception as r2_ex:
            logger.warning(f"[pricing.webhook] failed to save invoice to R2: {r2_ex}")
        
        # --- Step 12: Send invoice email to user ---
        try:
            # Get user email
            user_email = ""
            try:
                user_record = db.query(User).filter(User.uid == uid).first()
                if user_record and user_record.email:
                    user_email = user_record.email
            except Exception:
                pass
            
            # Fallback to email from webhook payload
            if not user_email:
                user_email = _first_email_from_payload(payload) or _first_email_from_payload(event_obj or {})
            
            if user_email:
                # Format amount for display
                amount_display = f"${(amount_cents / 100):.2f} {currency}" if amount_cents else "$0.00 USD"
                
                # Get frontend URL
                frontend_origin = os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud"
                billing_url = f"{frontend_origin}/billing"
                
                # Plan features based on plan type
                features = []
                if plan == "individual":
                    features = [
                        "All available tools",
                        "Up to 100,000 photos",
                        "Priority support",
                        "All future updates",
                    ]
                elif plan == "studios":
                    features = [
                        "All available tools",
                        "Unlimited photos",
                        "Team workflow tools",
                        "Priority support",
                    ]
                elif plan == "golden":
                    features = [
                        "All Studios features",
                        "Unlimited everything",
                        "No recurring payments",
                        "Lifetime updates",
                    ]
                
                # Render email template
                html = render_email(
                    "invoice_notification.html",
                    subject_line="Payment Receipt",
                    heading="🎉 Thank you for your purchase!",
                    subheading=f"Your payment has been processed successfully. Welcome to {plan_display}!",
                    invoice_id=invoice_id,
                    invoice_date=payment_date,
                    plan_display=plan_display,
                    billing_cycle=billing_cycle.title() if billing_cycle else None,
                    amount=amount_display,
                    is_golden=(plan == "golden"),
                    features=features,
                    billing_url=billing_url,
                    download_url=invoice_url or None,
                )
                
                # Send email
                send_email_smtp(
                    to_addr=user_email,
                    subject=f"Payment Receipt - {plan_display}",
                    html=html,
                    from_addr="billing@photomark.cloud",
                    from_name="Photomark Billing",
                )
                logger.info(f"[pricing.webhook] sent invoice email to {user_email} for invoice {invoice_id}")
            else:
                logger.warning(f"[pricing.webhook] no email found for user {uid}, skipping invoice email")
        except Exception as email_ex:
            logger.warning(f"[pricing.webhook] failed to send invoice email: {email_ex}")
            # Don't fail the webhook if email fails
            
    except Exception as inv_err:
        logger.warning(f"[pricing.webhook] invoice save error: {inv_err}")

    logger.info(f"[pricing.webhook] completed upgrade: uid={uid} plan={plan}")
    return {"ok": True, "upgraded": True, "uid": uid, "plan": plan}
