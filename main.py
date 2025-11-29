from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os
import warnings

from core.config import logger  # type: ignore
import asyncio
import os
import httpx

# Silence a noisy Kornia FutureWarning (does not affect our watermark pipeline)
warnings.filterwarnings(
    "ignore",
    message=r"`torch\.cuda\.amp\.custom_fwd",
    category=FutureWarning,
    module=r"kornia\.feature\.lightglue"
)

# Routers
from routers import (
    images, photos, auth, convert, vaults, voice, collab,
    gallery_assistant, color_grading, admin, smart_resize,
    shop,
)  # type: ignore
try:
    from routers import image_compression  # type: ignore
except Exception as _ex:
    image_compression = None
try:
    from routers import denoise  # type: ignore
except Exception as _ex:
    denoise = None
try:
    from routers import relight  # type: ignore
except Exception as _ex:
    relight = None
try:
    from routers import hdr_merge  # type: ignore
except Exception as _ex:
    hdr_merge = None

# Background removal router
try:
    from routers import background_removal  # noqa: E402
except Exception as _ex:
    logger.warning(f"background_removal router import failed: {_ex}")
    background_removal = None

# Pricing checkout (server-side) removed in favor of client-side overlay

app = FastAPI(title="Photo Watermarker")

# ---- CORS setup ----
# Prefer ALLOWED_ORIGINS, but also support legacy env names used in .env
_default_origins = ",".join([
    "https://photomark.cloud",
    "https://www.photomark.cloud",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
])
_origins_env = os.getenv("ALLOWED_ORIGINS") or os.getenv("CORS_ORIGINS") or os.getenv("FRONTEND_ORIGIN") or _default_origins
ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(",") if o.strip()]
# Optional regex to match any photomark.cloud subdomain and scheme
_origin_regex_env = os.getenv("ALLOWED_ORIGINS_REGEX") or os.getenv("CORS_ORIGIN_REGEX") or r".*"
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=_origin_regex_env,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Security headers ---
@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    try:
        response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer-when-downgrade")
        csp = (
            "default-src 'self'; "
            "img-src 'self' data: blob: https:; "
            "style-src 'self' 'unsafe-inline' https:; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https:; "
            "font-src 'self' data: https:; "
            "connect-src 'self' https: http://localhost:5173 http://127.0.0.1:5173; "
            "frame-ancestors 'none'"
        )
        response.headers.setdefault("Content-Security-Policy", csp)
    except Exception:
        pass
    return response

try:
    _cwd = os.getcwd()
    ACME_CHALLENGE_DIR = os.path.join(_cwd, "data", "acme", ".well-known", "acme-challenge")
    os.makedirs(ACME_CHALLENGE_DIR, exist_ok=True)
    app.mount("/.well-known/acme-challenge", StaticFiles(directory=ACME_CHALLENGE_DIR), name="acme-challenge")
except Exception:
    pass

def _get_request_host(request: Request) -> str:
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "")
    host = (host.split(":")[0] or "").strip().lower().strip(".")
    return host

def _should_redirect_shop(shop) -> bool:
    try:
        dom = shop.domain or {}
        hostname = (dom.get('hostname') or "").strip()
        return bool(hostname)
    except Exception:
        return False

def _find_shop_by_host(db, host: str):
    try:
        from models.shop import Shop
        from sqlalchemy import cast, String, func
        host_l = (host or "").strip().lower().rstrip(".")
        q = db.query(Shop).filter(func.lower(cast(Shop.domain['hostname'], String)) == host_l)
        shop = q.first()
        if shop:
            return shop
        # Fallback: handle potential stored variations
        q2 = db.query(Shop).filter(cast(Shop.domain['hostname'], String).like(f"%{host_l}%"))
        return q2.first()
    except Exception:
        return None

@app.middleware("http")
async def custom_domain_routing(request: Request, call_next):
    try:
        try:
            path = request.url.path or ""
        except Exception:
            path = ""
        if path.startswith("/.well-known/acme-challenge/") or path.startswith("/shop/"):
            return await call_next(request)

        host = _get_request_host(request)
        if host:
            from core.database import get_db
            from sqlalchemy.orm import Session
            from models.shop import Shop
            db: Session = next(get_db())
            try:
                shop = _find_shop_by_host(db, host)
                if shop:
                    if _should_redirect_shop(shop):
                        slug = (shop.slug or "").strip()
                        front = (os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            r = await client.get(f"{front}/", follow_redirects=True)
                            html = r.text
                            inject = f"<script>try{{history.replaceState(null,'','/shop/{slug}')}}catch(e){{}}</script>" if slug else ""
                            html = html.replace("</head>", inject + "</head>") if "</head>" in html else (inject + html)
                            return Response(content=html, media_type="text/html", status_code=200)
            finally:
                try:
                    db.close()
                except Exception:
                    pass
    except Exception:
        pass
    return await call_next(request)

# ---- Static mount (local fallback) ----
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")



# ---- Include routers ----
app.include_router(images.router)
app.include_router(photos.router)
app.include_router(auth.router)
app.include_router(convert.router)
app.include_router(vaults.router)
app.include_router(voice.router)
app.include_router(collab.router)
# Gallery assistant (chat + actions)
app.include_router(gallery_assistant.router)
# Color grading (LUT)
app.include_router(color_grading.router)
# Mark agent with function calling and vision support
try:
    from routers import mark_agent  # noqa: E402
    app.include_router(mark_agent.router)
except Exception as _ex:
    logger.warning(f"mark_agent router not available: {_ex}")
app.include_router(smart_resize.router)
app.include_router(shop.router)
from routers import pexels  # noqa: E402
app.include_router(pexels.router)
if image_compression is not None:
    app.include_router(image_compression.router)
if denoise is not None:
    app.include_router(denoise.router)
if relight is not None:
    app.include_router(relight.router)
if hdr_merge is not None:
    app.include_router(hdr_merge.router)

# app.include_router(pricing_checkout.router)  # removed
# embed iframe endpoints
from routers import embed  # noqa: E402
app.include_router(embed.router)
# extra endpoints for frontend compatibility
from routers import upload, device  # noqa: E402
app.include_router(upload.router)
app.include_router(device.router)
# backups router (Backblaze B2)
try:
    from routers import backup  # noqa: E402
    app.include_router(backup.router)
except Exception as _ex:
    logger.warning(f"backup router not available: {_ex}")
# admin endpoints
app.include_router(admin.router)
# Removed: bookings, portfolio, and create_lut endpoints

# legacy style LUT GPU endpoint
try:
    from routers import style_lut  # noqa: E402
    app.include_router(style_lut.router)
except Exception as _ex:
    logger.warning(f"style_lut router not available: {_ex}")

# style histogram matching endpoint
try:
    from routers import style_hist  # noqa: E402
    app.include_router(style_hist.router)
except Exception as _ex:
    logger.warning(f"style_hist router not available: {_ex}")

# new endpoints for signup and account email change
from routers import auth_ip, account  # noqa: E402
app.include_router(auth_ip.router)
app.include_router(account.router)

# retouch endpoints (AI background)

# retouch result upload endpoint

# shared upload endpoint (for marked photos)
from routers import shared_upload  # noqa: E402
app.include_router(shared_upload.router)

# background removal endpoints
if background_removal is not None:
    app.include_router(background_removal.router)

# Moodboard generator
try:
    from routers import moodboard  # noqa: E402
    app.include_router(moodboard.router)
except Exception as _ex:
    logger.warning(f"moodboard router not available: {_ex}")

# Stable Diffusion img2img endpoint removed

# instructions-based edit tool removed

# prelaunch subscription endpoint
try:
    from routers import prelaunch  # noqa: E402
    app.include_router(prelaunch.router)
except Exception as _ex:
    logger.warning(f"prelaunch router not available: {_ex}")



# affiliate endpoints (secret invite sender)
from routers import affiliates  # noqa: E402
app.include_router(affiliates.router)

# portfolios endpoints (owner showcase)
from routers import portfolios  # noqa: E402
app.include_router(portfolios.router)

# Pricing webhook (replaces legacy Dodo webhook)
try:
    from routers import pricing_webhook  # noqa: E402
    app.include_router(pricing_webhook.router)

    # Backward-compatible Dodo webhook path
    from routers.pricing_webhook import pricing_webhook as _pricing_webhook_handler  # type: ignore

    @app.post("/api/payments/dodo/webhook")
    async def dodo_webhook(request: Request):
        return await _pricing_webhook_handler(request)
except Exception as _ex:
    logger.warning(f"pricing webhook router not available: {_ex}")

# outreach email endpoint (photographer/artist introduction)
from routers import outreach  # noqa: E402
app.include_router(outreach.router)

# inbound email replies + list for UI
from routers import replies  # noqa: E402
app.include_router(replies.router)

# lens simulation tool removed

# product updates (changelog + email broadcast)
try:
    from routers import updates  # noqa: E402
    app.include_router(updates.router)
except Exception as _ex:
    logger.warning(f"updates router not available: {_ex}")

# Billing info (Neon-backed)
try:
    from routers import billing  # noqa: E402
    app.include_router(billing.router)
except Exception as _ex:
    logger.warning(f"billing router not available: {_ex}")



@app.get("/api/allow-domain")
async def allow_domain(request: Request):
    # Caddy on_demand_tls ask endpoint: returns 200 if domain is allowed
    from core.database import get_db  # lazy import to avoid circulars
    from sqlalchemy.orm import Session
    from models.shop import Shop

    domain = (request.query_params.get("domain") or request.query_params.get("host") or "").strip().lower()
    if not domain:
        return {"allow": False}
    try:
        db: Session = next(get_db())
        from sqlalchemy import cast, String
        shop = db.query(Shop).filter(cast(Shop.domain['hostname'], String) == domain).first()
        if shop:
            try:
                enabled = bool((shop.domain or {}).get('enabled') or False)
            except Exception:
                enabled = False
            if enabled:
                return {"allow": True}
    except Exception as _ex:
        logger.warning(f"allow-domain check failed: {_ex}")
    return {"allow": False}

@app.get("/api/domains/validate")
async def domains_validate(request: Request):
    from fastapi.responses import PlainTextResponse
    domain = (request.query_params.get("domain") or request.query_params.get("host") or "").strip().lower().rstrip(".")
    if not domain:
        return PlainTextResponse("no", status_code=403)
    try:
        from core.database import get_db
        from sqlalchemy.orm import Session
        from models.shop import Shop
        db: Session = next(get_db())
        try:
            from sqlalchemy import cast, String
            shop = db.query(Shop).filter(cast(Shop.domain['hostname'], String) == domain).first()
            if shop:
                enabled = bool((shop.domain or {}).get('enabled') or False)
                dns_verified = bool((shop.domain or {}).get('dnsVerified') or False)
                if enabled or dns_verified:
                    return PlainTextResponse("ok", status_code=200)
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception:
        pass
    return PlainTextResponse("no", status_code=403)
async def _check_domain(hostname: str):
    dns_verified = False
    cname_target = None
    ssl_error = None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"https://cloudflare-dns.com/dns-query?name={hostname}&type=CNAME",
                headers={"Accept": "application/dns-json"}
            )
            data = r.json()
            answers = data.get("Answer") or []
            for ans in answers:
                if (ans.get("type") == 5) and ans.get("data"):
                    cname_target = (ans["data"] or "").strip(".").lower()
                    if cname_target == "api.photomark.cloud":
                        dns_verified = True
                        break
    except Exception:
        dns_verified = False
    ssl_status = "unknown"
    if dns_verified:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                h = await client.head(f"https://{hostname}", follow_redirects=True)
                ssl_status = "active" if h.status_code < 400 else "pending"
        except Exception as e:
            ssl_error = str(e)
            ssl_status = "pending"
    else:
        ssl_status = "blocked"
    return {
        "dnsVerified": dns_verified,
        "sslStatus": ssl_status,
        "cnameObserved": cname_target,
        "error": ssl_error,
    }
async def _domain_check_once():
    from sqlalchemy.orm import Session
    from models.shop import Shop
    from core.database import get_db
    from core.auth import get_user_email_from_uid
    from utils.emailing import render_email, send_email_smtp
    db: Session = next(get_db())
    try:
        shops = db.query(Shop).all()
        for s in shops:
            dom = s.domain or {}
            hostname = (dom.get("hostname") or "").strip().lower()
            if not hostname:
                continue
            prev_dns = bool(dom.get("dnsVerified"))
            prev_ssl = str(dom.get("sslStatus") or "")
        res = await _check_domain(hostname)
        changed = (prev_dns != res["dnsVerified"]) or (prev_ssl != res["sslStatus"])
        s.domain = {
            "hostname": hostname,
            "dnsTarget": "api.photomark.cloud",
            "dnsVerified": res["dnsVerified"],
            "sslStatus": res["sslStatus"],
            "lastChecked": _now_iso(),
            "cnameObserved": res["cnameObserved"],
            "error": res["error"],
            "enabled": bool(dom.get("enabled") or False),
        }
        s.updated_at = _now()
        if changed:
            try:
                email = get_user_email_from_uid(s.owner_uid) or ""
                if email:
                    subject = "Domain status updated"
                    html = render_email(
                        "email_basic.html",
                        title="Domain status",
                        intro=f"{hostname}: DNS={res['dnsVerified']}, SSL={res['sslStatus']}",
                        button_label="Open shop",
                        button_url=f"https://{hostname}",
                    )
                    text = f"{hostname} DNS={res['dnsVerified']} SSL={res['sslStatus']}"
                    send_email_smtp(email, subject, html, text)
            except Exception:
                pass
        db.commit()
    finally:
        db.close()
def _now():
    from datetime import datetime
    return datetime.utcnow()
def _now_iso():
    return _now().isoformat()
async def _domain_scheduler_loop():
    interval = int((os.getenv("DOMAIN_CHECK_INTERVAL_SEC") or "600").strip() or "600")
    while True:
        try:
            await _domain_check_once()
        except Exception:
            pass
        await asyncio.sleep(interval)
@app.on_event("startup")
async def _start_domain_scheduler():
    flag = (os.getenv("RUN_DOMAIN_SCHEDULER") or "0").strip()
    if flag == "1":
        asyncio.create_task(_domain_scheduler_loop())

@app.on_event("startup")
async def _init_postgres_schema():
    try:
        from core.database import init_db
        init_db()
    except Exception as _ex:
        logger.warning(f"init_db failed: {_ex}")
@app.get("/")
async def root(request: Request):
    try:
        host = _get_request_host(request)
        if host:
            from core.database import get_db
            from sqlalchemy.orm import Session
            from models.shop import Shop
            from models.user import User
            db: Session = next(get_db())
            try:
                shop = _find_shop_by_host(db, host)
                if shop:
                    if _should_redirect_shop(shop):
                        user = db.query(User).filter(User.uid == shop.owner_uid).first()
                        sub_id = (user.subscription_id if user and user.subscription_id else "")
                        status = (user.subscription_status if user and user.subscription_status else (user.plan if user and user.plan else "inactive"))
                        slug = (shop.slug or "").strip()
                        front = (os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            r = await client.get(f"{front}/", follow_redirects=True)
                            html = r.text
                            inject = f"<script>try{{history.replaceState(null,'','/shop/{slug}')}}catch(e){{}}</script>" if slug else ""
                            html = html.replace("</head>", inject + "</head>") if "</head>" in html else (inject + html)
                            return Response(content=html, media_type="text/html", status_code=200)
            finally:
                try:
                    db.close()
                except Exception:
                    pass
    except Exception:
        pass
    return {"ok": True}

# Catch-all: redirect any unmatched path on a configured custom domain to the public shop
@app.get("/{remaining_path:path}")
async def domain_redirect_any(request: Request, remaining_path: str):
    try:
        host = _get_request_host(request)
        if host:
            from core.database import get_db
            from sqlalchemy.orm import Session
            from models.shop import Shop
            from models.user import User
            db: Session = next(get_db())
            try:
                shop = _find_shop_by_host(db, host)
                if shop:
                    if _should_redirect_shop(shop):
                        user = db.query(User).filter(User.uid == shop.owner_uid).first()
                        slug = (shop.slug or "").strip()
                        front = (os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            r = await client.get(f"{front}/", follow_redirects=True)
                            html = r.text
                            inject = f"<script>try{{history.replaceState(null,'','/shop/{slug}')}}catch(e){{}}</script>" if slug else ""
                            html = html.replace("</head>", inject + "</head>") if "</head>" in html else (inject + html)
                            return Response(content=html, media_type="text/html", status_code=200)
            finally:
                try:
                    db.close()
                except Exception:
                    pass
    except Exception:
        pass
    return {"ok": True}

@app.get("/allow-domain")
async def allow_domain_text(request: Request):
    from fastapi.responses import PlainTextResponse
    domain = request.query_params.get("domain")
    if not domain:
        return PlainTextResponse("no", status_code=403)
    domain = domain.strip().lower().rstrip(".")
    try:
        from core.database import get_db
        from sqlalchemy.orm import Session
        from models.shop import Shop
        db: Session = next(get_db())
        try:
            shop = _find_shop_by_host(db, domain)
            if shop:
                return PlainTextResponse("yes", status_code=200)
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception:
        pass
    return PlainTextResponse("no", status_code=403)

@app.get("/resolve-domain/{hostname}")
async def resolve_domain_simple(hostname: str):
    from fastapi import HTTPException
    inbound = (hostname or "").strip().lower().rstrip(".")
    if not inbound:
        raise HTTPException(status_code=400, detail="Invalid hostname")
    try:
        from core.database import get_db
        from sqlalchemy.orm import Session
        from models.shop import Shop
        db: Session = next(get_db())
        try:
            shop = _find_shop_by_host(db, inbound)
            if not shop:
                raise HTTPException(status_code=404, detail="No shop bound to this domain")
            enabled = bool((shop.domain or {}).get('enabled') or False)
            return {
                "slug": (shop.slug or "").strip(),
                "uid": shop.uid,
                "domain": (shop.domain or {}),
                "enabled": enabled,
            }
        finally:
            try:
                db.close()
            except Exception:
                pass
    except HTTPException:
        raise
    except Exception as _ex:
        raise HTTPException(status_code=500, detail=f"Failed to resolve domain: {_ex}")

@app.get("/shop")
async def proxy_shop_root():
    try:
        front = (os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{front}/shop")
            return Response(content=r.text, media_type="text/html", status_code=r.status_code)
    except Exception:
        return {"ok": True}

@app.get("/shop/{slug}")
async def proxy_shop_slug(slug: str):
    try:
        front = (os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{front}/shop/{slug}")
            return Response(content=r.text, media_type="text/html", status_code=r.status_code)
    except Exception as _ex:
        return Response(content=f"<html><body><h1>Shop page unavailable</h1><p>{_ex}</p></body></html>", media_type="text/html", status_code=502)

@app.get("/assets/{path:path}")
async def proxy_assets(path: str):
    try:
        front = (os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{front}/assets/{path}")
            ct = r.headers.get("content-type") or "application/octet-stream"
            return Response(content=r.content, media_type=ct, status_code=r.status_code)
    except Exception:
        return Response(content=b"", media_type="application/octet-stream", status_code=404)
