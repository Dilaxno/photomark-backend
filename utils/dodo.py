import os
import asyncio
import httpx
from typing import Dict, Any, Optional, Tuple
from core.config import logger, DODO_API_BASE, DODO_CHECKOUT_PATH, DODO_API_KEY

# Build standard headers list including variants used across integrations
def build_headers_list() -> list[dict]:
    api_key = (DODO_API_KEY or "").strip()
    headers_list = [
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "PhotomarkBackend/1.0",
        },
        {
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "PhotomarkBackend/1.0",
        },
    ]
    # Optional environment/business/brand
    business_id = (os.getenv("DODO_BUSINESS_ID") or "").strip()
    brand_id = (os.getenv("DODO_BRAND_ID") or "").strip()
    env_hdr = (os.getenv("DODO_PAYMENTS_ENVIRONMENT") or os.getenv("DODO_ENV") or "").strip().strip('"')
    # Sensible default for test domains
    if not env_hdr:
        try:
            base = (DODO_API_BASE or "").lower()
            if "test.dodopayments.com" in base or "sandbox" in base:
                env_hdr = "sandbox"
        except Exception:
            pass
    # Normalize environment header for test/live
    try:
        base = (DODO_API_BASE or "").lower()
        if env_hdr.lower() == "test" and ("test.dodopayments.com" in base or "sandbox" in base):
            env_hdr = "sandbox"
        if env_hdr.lower() == "prod":
            env_hdr = "production"
    except Exception:
        pass

    for h in headers_list:
        if business_id:
            h["Dodo-Business-Id"] = business_id
        if brand_id:
            h["Dodo-Brand-Id"] = brand_id
        if env_hdr:
            h["Dodo-Environment"] = env_hdr
    return headers_list


def build_endpoints() -> list[str]:
    # Default to Dodo test base if env not set; override via DODO_API_BASE
    # Valid bases: https://test.dodopayments.com (sandbox), https://live.dodopayments.com (production)
    # Normalize base URL. Never use placeholder/example or api.dodopayments.com
    base_in = (DODO_API_BASE or "").strip()
    low = base_in.lower()
    if (not base_in) or ("example" in low) or ("api.dodo-payments" in low) or ("api.dodopayments.com" in low):
        base = "https://test.dodopayments.com"
    else:
        base = base_in
    base = base.rstrip("/")

    path = (DODO_CHECKOUT_PATH or "/v1/payment-links").strip()
    if not path.startswith("/"):
        path = "/" + path
    logger.info(f"[dodo] using api base: {base}")
    # Use stable, documented endpoints only; avoid legacy paths that can trip Cloudflare
    endpoints: list[str] = [
        f"{base}/v1/checkout-sessions",
        f"{base}/v1/payments",
        f"{base}/v1/payment-links",
    ]
    # Include configured path only if it looks like a v1 API path
    if path.startswith("/v1/"):
        endpoints.append(f"{base}{path}")
    else:
        try:
            logger.warning(f"[dodo] ignoring non-v1 checkout path '{path}' (using defaults)")
        except Exception:
            pass
    return endpoints


def pick_checkout_url(data: Dict[str, Any]) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    # Common fields for session or link creation responses
    link = (
        data.get("session_url")
        or data.get("checkout_url")
        or data.get("url")
        or data.get("payment_link")
    )
    if link:
        return str(link)
    obj = data.get("data") if isinstance(data, dict) else None
    if isinstance(obj, dict):
        inner = (
            obj.get("session_url")
            or obj.get("checkout_url")
            or obj.get("url")
            or obj.get("payment_link")
            or ""
        )
        return str(inner) or None
    return None


async def create_checkout_link(payloads: list[dict]) -> Tuple[Optional[str], Optional[dict]]:
    """Try multiple endpoints, header variants, and payload shapes to create a checkout link.
    Returns (link, error_details). If link is None, error_details contains last failure.
    """
    endpoints = build_endpoints()
    headers_list = build_headers_list()

    # Add success redirect URL to all payloads
    redirect_url = "https://photomark.cloud"
    updated_payloads = []
    for p in payloads:
        new_p = p.copy()
        # Common naming patterns for payment providers
        new_p.setdefault("success_url", redirect_url)
        new_p.setdefault("return_url", redirect_url)
        new_p.setdefault("redirect_url", redirect_url)
        # Do not force payment_link; keep payload minimal to satisfy checkout-sessions schema
        updated_payloads.append(new_p)

    last_error = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        for url in endpoints:
            for headers in headers_list:
                for payload in updated_payloads:
                    try:
                        logger.info(f"[dodo] creating payment link via {url} with headers {list(headers.keys())}")
                        resp = await client.post(url, headers=headers, json=payload)
                        if resp.status_code in (200, 201):
                            try:
                                data = resp.json()
                            except Exception:
                                data = {}
                            link = pick_checkout_url(data)
                            if link:
                                logger.info("[dodo] created payment link successfully")
                                return link, None
                        # Handle rate limiting / Cloudflare
                        if resp.status_code == 429:
                            try:
                                body_text = resp.text
                            except Exception:
                                body_text = ""
                            last_error = {
                                "status": resp.status_code,
                                "endpoint": url,
                                "payload_keys": list(payload.keys()),
                                "body": body_text[:2000],
                            }
                            logger.warning(f"[dodo] rate limited at {url}; backing off briefly")
                            await asyncio.sleep(0.8)
                            continue
                        # Other non-success
                        try:
                            body_text = resp.text
                        except Exception:
                            body_text = ""
                        last_error = {
                            "status": resp.status_code,
                            "endpoint": url,
                            "payload_keys": list(payload.keys()),
                            "body": body_text[:2000],
                        }
                    except Exception as ex:
                        last_error = {"exception": str(ex), "endpoint": url, "payload_keys": list(payload.keys())}
    if last_error:
        logger.warning(f"[dodo] checkout link creation failed: {last_error}")
    return None, last_error


async def create_checkout_session(payload: dict) -> Tuple[Optional[dict], Optional[dict]]:
    base = (DODO_API_BASE or "https://test.dodopayments.com").rstrip("/")
    headers_list = build_headers_list()
    endpoints = [
        f"{base}/v1/checkout-sessions",
        f"{base}/checkout-sessions",
        f"{base}/v1/checkouts",
        f"{base}/checkouts",
    ]
    last_error = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        for url in endpoints:
            for headers in headers_list:
                try:
                    logger.info(f"[dodo] creating checkout session via {url} with headers {list(headers.keys())}")
                    resp = await client.post(url, headers=headers, json=payload)
                    if resp.status_code in (200, 201):
                        try:
                            data = resp.json()
                        except Exception:
                            data = {}
                        return data, None
                    try:
                        body_text = resp.text
                    except Exception:
                        body_text = ""
                    last_error = {
                        "status": resp.status_code,
                        "endpoint": url,
                        "payload_keys": list(payload.keys()),
                        "body": body_text[:2000],
                    }
                    if resp.status_code == 404:
                        continue
                except Exception as ex:
                    last_error = {"exception": str(ex), "endpoint": url, "payload_keys": list(payload.keys())}
    if last_error:
        logger.warning(f"[dodo] checkout session creation failed: {last_error}")
    return None, last_error


def _build_subscription_endpoints(subscription_id: str) -> list[str]:
    """
    Try common PATCH endpoints for updating/cancelling a subscription.
    """
    base = (DODO_API_BASE or "https://api.dodopayments.com").rstrip("/")
    sid = subscription_id.strip()
    return [
        f"{base}/v1/subscriptions/{sid}",
        f"{base}/subscriptions/{sid}",
        f"{base}/api/subscriptions/{sid}",
    ]


async def cancel_subscription_immediately(subscription_id: str) -> Tuple[bool, Optional[dict]]:
    """
    Attempt to cancel a Dodo subscription immediately (no period-end grace).
    Returns (ok, last_error). Idempotent: already-cancelled treated as ok.
    """
    if not subscription_id:
        return False, {"error": "missing_subscription_id"}

    payload_variants = [
        {"status": "cancelled"},
        {"status": "cancelled", "cancel_at_next_billing_date": False},
        {"cancel_at_next_billing_date": False, "status": "cancelled"},
    ]
    headers_list = build_headers_list()
    endpoints = _build_subscription_endpoints(subscription_id)

    last_error: Optional[dict] = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        for url in endpoints:
            for headers in headers_list:
                for body in payload_variants:
                    try:
                        logger.info(f"[dodo] cancelling subscription {subscription_id} via {url}")
                        resp = await client.patch(url, headers=headers, json=body)
                        if resp.status_code in (200, 204):
                            return True, None
                        # Treat already-cancelled or not-active as success
                        try:
                            text = resp.text or ""
                        except Exception:
                            text = ""
                        low = text.lower()
                        if any(x in low for x in ("already cancelled", "already_cancell", "status\":\"cancelled", "status\": \"cancelled")):
                            return True, None
                        last_error = {"status": resp.status_code, "endpoint": url, "body": text[:2000]}
                    except Exception as ex:
                        last_error = {"exception": str(ex), "endpoint": url}
    return False, last_error
