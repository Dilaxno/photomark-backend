from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Request, Depends, Body
from fastapi.responses import JSONResponse
from core.auth import get_uid_from_request
from core.database import get_db
from models.shop import Shop, ShopSlug
from sqlalchemy.orm import Session
import os
import uuid
import base64
from datetime import datetime
import re
import httpx
from utils.storage import upload_bytes

router = APIRouter(prefix="/api/shop", tags=["shop"])

# Allowed file types
IMAGE_TYPES = ['image/jpeg', 'image/jpg', 'image/png', 'image/gif', 'image/webp']
VIDEO_TYPES = ['video/mp4', 'video/webm', 'video/quicktime', 'video/x-msvideo']
FONT_TYPES = ['font/ttf', 'font/otf', 'font/woff', 'font/woff2', 'application/font-ttf', 'application/font-otf', 'application/font-woff', 'application/font-woff2', 'application/octet-stream']

# File size limits
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_VIDEO_SIZE = 100 * 1024 * 1024  # 100MB
MAX_FONT_SIZE = 20 * 1024 * 1024   # 20MB

print("[SHOP] Shop router initialized")


@router.post('/upload')
async def upload_shop_asset(
    request: Request,
    file: UploadFile = File(...),
    type: str = Form(...),
    shop_id: str = Form(...)
):
    """
    Upload shop assets (logo, pfp, banner, product images/videos).
    """
    print(f"\n[SHOP UPLOAD] Starting upload - Type: {type}, File: {file.filename}")
    
    try:
        # Verify authentication
        uid = get_uid_from_request(request)
        if not uid:
            print("[SHOP UPLOAD] ERROR: No authenticated user")
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        print(f"[SHOP UPLOAD] User authenticated: {uid}")
        
        # Validate upload type
        if type in ['logo', 'pfp', 'banner', 'product_image']:
            allowed_types = IMAGE_TYPES
            max_size = MAX_IMAGE_SIZE
            media_type = 'image'
        elif type == 'product_video':
            allowed_types = VIDEO_TYPES
            max_size = MAX_VIDEO_SIZE
            media_type = 'video'
        elif type == 'font':
            allowed_types = FONT_TYPES
            max_size = MAX_FONT_SIZE
            media_type = 'font'
        else:
            raise HTTPException(status_code=400, detail=f"Invalid upload type: {type}")
        
        # Validate content type
        if file.content_type not in allowed_types:
            print(f"[SHOP UPLOAD] ERROR: Invalid content type: {file.content_type}")
            raise HTTPException(
                status_code=400,
                detail=f"Invalid file type. Expected {media_type}, got {file.content_type}"
            )
        
        # Read and validate file size
        contents = await file.read()
        file_size = len(contents)
        
        print(f"[SHOP UPLOAD] File size: {file_size / (1024 * 1024):.2f}MB")
        
        if file_size > max_size:
            max_mb = max_size / (1024 * 1024)
            raise HTTPException(
                status_code=400,
                detail=f"File too large. Maximum size is {max_mb}MB"
            )
        
        # Generate a unique filename for reference
        file_extension = file.filename.split('.')[-1] if '.' in file.filename else 'jpg'
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        unique_id = str(uuid.uuid4())[:8]
        filename = f"{type}_{timestamp}_{unique_id}.{file_extension}"

        date_prefix = datetime.utcnow().strftime('%Y/%m/%d')
        folder = {
            'logo': 'logo',
            'pfp': 'pfp',
            'banner': 'banner',
            'product_image': 'products/images',
            'product_video': 'products/videos',
            'font': 'fonts',
        }.get(type, 'misc')
        object_key = f"shops/{shop_id}/{folder}/{date_prefix}/{filename}"
        url = upload_bytes(object_key, contents, content_type=file.content_type or 'application/octet-stream')

        # Generate base64 data URL for immediate preview (do not store in DB)
        base64_data = base64.b64encode(contents).decode('utf-8')
        data_url = f"data:{file.content_type};base64,{base64_data}"

        print(f"[SHOP UPLOAD] Success - Generated: {filename} -> {object_key}")

        return JSONResponse({
            'success': True,
            'url': url,
            'preview_url': data_url,
            'filename': filename,
            'type': type,
            'size': file_size,
            'content_type': file.content_type
        })
        
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"[SHOP UPLOAD] UNEXPECTED ERROR: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


@router.get('/{uid}')
async def get_shop_by_uid(uid: str, db: Session = Depends(get_db)):
    """Get shop data by owner UID"""
    try:
        shop = db.query(Shop).filter(Shop.uid == uid).first()
        if not shop:
            raise HTTPException(status_code=404, detail="Shop not found")
        return shop.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get shop: {str(e)}")


@router.get('/slug/{slug}')
async def get_shop_by_slug(slug: str, db: Session = Depends(get_db)):
    """Get shop data by slug (for public viewing)"""
    try:
        # Look up UID from slug mapping
        slug_mapping = db.query(ShopSlug).filter(ShopSlug.slug == slug).first()
        if not slug_mapping:
            raise HTTPException(status_code=404, detail="Shop not found")
        
        # Get shop data
        shop = db.query(Shop).filter(Shop.uid == slug_mapping.uid).first()
        if not shop:
            raise HTTPException(status_code=404, detail="Shop not found")
        
        return shop.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get shop: {str(e)}")

def _normalize_domain(dom: str | None) -> str | None:
    if not dom:
        return None
    return dom.strip().lower().rstrip('.')

@router.get('/resolve-domain/{hostname}')
async def resolve_domain(hostname: str, db: Session = Depends(get_db)):
    inbound = _normalize_domain(hostname)
    if not inbound:
        raise HTTPException(status_code=400, detail="Invalid hostname")
    try:
        from sqlalchemy import cast, String
        shop = db.query(Shop).filter(cast(Shop.domain['hostname'], String) == inbound).first()
        if not shop:
            raise HTTPException(status_code=404, detail="No shop bound to this domain")
        enabled = bool((shop.domain or {}).get('enabled') or False)
        return {
            "slug": (shop.slug or "").strip(),
            "uid": shop.uid,
            "domain": (shop.domain or {}),
            "enabled": enabled,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to resolve domain: {str(e)}")

@router.get('/subscription/{sub_id}')
async def get_shop_by_subscription(sub_id: str, db: Session = Depends(get_db)):
    try:
        from models.user import User
        u = db.query(User).filter(User.subscription_id == sub_id).first()
        if not u:
            raise HTTPException(status_code=404, detail="Owner not found")
        shop = db.query(Shop).filter(Shop.uid == u.uid).first()
        if not shop:
            shop = db.query(Shop).filter(Shop.owner_uid == u.uid).first()
        if not shop:
            raise HTTPException(status_code=404, detail="Shop not found")
        return shop.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get shop: {str(e)}")


@router.post('/settings')
async def save_shop_settings(
    request: Request,
    settings: dict = Body(...),
    db: Session = Depends(get_db)
):
    """Save shop settings (name, slug, description, theme)"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        shop = db.query(Shop).filter(Shop.uid == uid).first()
        now = datetime.utcnow()
        
        if shop:
            # Update existing shop
            shop.name = settings.get('name', shop.name)
            shop.slug = settings.get('slug', shop.slug)
            shop.description = settings.get('description', shop.description)
            shop.owner_name = settings.get('ownerName', shop.owner_name)
            shop.theme = settings.get('theme', shop.theme)
            shop.updated_at = now
        else:
            # Create new shop
            shop = Shop(
                uid=uid,
                name=settings.get('name', 'My Shop'),
                slug=settings.get('slug', uid),
                description=settings.get('description', ''),
                owner_uid=uid,
                owner_name=settings.get('ownerName'),
                theme=settings.get('theme', {}),
                products=[],
                created_at=now,
                updated_at=now
            )
            db.add(shop)
        
        # Update slug mapping
        slug_mapping = db.query(ShopSlug).filter(ShopSlug.slug == shop.slug).first()
        if slug_mapping:
            slug_mapping.uid = uid
            slug_mapping.updated_at = now
        else:
            slug_mapping = ShopSlug(slug=shop.slug, uid=uid, updated_at=now)
            db.add(slug_mapping)
        
        db.commit()
        return {"success": True, "message": "Shop settings saved"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save settings: {str(e)}")


@router.post('/products')
async def save_shop_products(
    request: Request,
    products: list = Body(...),
    db: Session = Depends(get_db)
):
    """Save shop products"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        shop = db.query(Shop).filter(Shop.uid == uid).first()
        if not shop:
            raise HTTPException(status_code=404, detail="Shop not found. Create settings first.")
        
        shop.products = products
        shop.updated_at = datetime.utcnow()
        db.commit()
        
        return {"success": True, "message": "Products saved"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save products: {str(e)}")


@router.post('/data')
async def save_shop_data(
    request: Request,
    data: dict = Body(...),
    db: Session = Depends(get_db)
):
    """Save complete shop data (settings + products)"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        settings = data.get('settings', {})
        products = data.get('products', [])
        
        shop = db.query(Shop).filter(Shop.uid == uid).first()
        now = datetime.utcnow()
        
        if shop:
            # Update existing shop
            shop.name = settings.get('name', shop.name)
            shop.slug = settings.get('slug', shop.slug)
            shop.description = settings.get('description', shop.description)
            shop.owner_name = settings.get('ownerName', shop.owner_name)
            shop.theme = settings.get('theme', shop.theme)
            shop.products = products
            shop.updated_at = now
        else:
            # Create new shop
            shop = Shop(
                uid=uid,
                name=settings.get('name', 'My Shop'),
                slug=settings.get('slug', uid),
                description=settings.get('description', ''),
                owner_uid=uid,
                owner_name=settings.get('ownerName'),
                theme=settings.get('theme', {}),
                products=products,
                created_at=now,
                updated_at=now
            )
            db.add(shop)
        
        # Update slug mapping
        slug_mapping = db.query(ShopSlug).filter(ShopSlug.slug == shop.slug).first()
        if slug_mapping:
            slug_mapping.uid = uid
            slug_mapping.updated_at = now
        else:
            slug_mapping = ShopSlug(slug=shop.slug, uid=uid, updated_at=now)
            db.add(slug_mapping)
        
        db.commit()
        return {"success": True, "message": "Shop data saved"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save shop data: {str(e)}")


@router.post('/slug')
async def update_slug_mapping(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db)
):
    """Update slug mapping for a shop"""
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    slug = payload.get('slug')
    if not slug:
        raise HTTPException(status_code=400, detail="Slug is required")
    
    try:
        # Check if slug is already taken by another user
        existing = db.query(ShopSlug).filter(ShopSlug.slug == slug).first()
        if existing and existing.uid != uid:
            raise HTTPException(status_code=409, detail="Slug already taken")
        
        # Update or create slug mapping
        if existing:
            existing.updated_at = datetime.utcnow()
        else:
            slug_mapping = ShopSlug(slug=slug, uid=uid, updated_at=datetime.utcnow())
            db.add(slug_mapping)
        
        db.commit()
        return {"success": True, "message": "Slug mapping updated"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update slug: {str(e)}")


@router.post('/domain')
async def set_custom_domain(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db)
):
    """Configure a custom domain/subdomain for the user's shop.
    Stores desired hostname and initializes status tracking.
    """
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")

    hostname = (payload.get('hostname') or '').strip().lower()
    if not hostname:
        raise HTTPException(status_code=400, detail="hostname is required")

    # Basic hostname validation
    if len(hostname) > 255 or not re.match(r'^[a-z0-9.-]+$', hostname):
        raise HTTPException(status_code=400, detail="invalid hostname")

    try:
        shop = db.query(Shop).filter(Shop.uid == uid).first()
        if not shop:
            raise HTTPException(status_code=404, detail="Shop not found")

        existing = (shop.domain or {})
        current_host = str((existing.get('hostname') or '')).strip().lower()
        if current_host and current_host != hostname:
            raise HTTPException(status_code=409, detail="domain_already_set")

        now = datetime.utcnow().isoformat()
        shop.domain = {
            "hostname": hostname,
            "dnsTarget": "api.photomark.cloud",
            "dnsVerified": False,
            "sslStatus": "unknown",
            "lastChecked": now,
            "enabled": False,
        }
        shop.updated_at = datetime.utcnow()
        db.commit()

        instructions = {
            "recordType": "CNAME",
            "name": hostname,
            "value": "api.photomark.cloud",
            "ttl": 300
        }

        return {
            "success": True,
            "message": "Custom domain saved. Create the CNAME record and check status.",
            "instructions": instructions,
            "domain": shop.domain
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to set custom domain: {str(e)}")


@router.post('/domain/remove')
async def remove_custom_domain(request: Request, db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        shop = db.query(Shop).filter(Shop.uid == uid).first()
        if not shop:
            raise HTTPException(status_code=404, detail="Shop not found")
        shop.domain = {}
        shop.updated_at = datetime.utcnow()
        db.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to remove domain: {str(e)}")


@router.post('/domain/enable')
async def enable_custom_domain(request: Request, db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        shop = db.query(Shop).filter(Shop.uid == uid).first()
        if not shop:
            raise HTTPException(status_code=404, detail="Shop not found")
        dom = dict(shop.domain or {})
        hostname = str(dom.get('hostname') or '').strip().lower()
        if not hostname:
            raise HTTPException(status_code=400, detail="No hostname configured")
        dns_ok = bool(dom.get('dnsVerified'))
        ssl_ok = str(dom.get('sslStatus') or '').strip().lower() == 'active'
        if not (dns_ok and ssl_ok):
            raise HTTPException(status_code=412, detail="domain_not_ready")
        dom['enabled'] = True
        shop.domain = dom
        shop.updated_at = datetime.utcnow()
        db.commit()
        return {"ok": True, "domain": shop.domain}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to enable domain: {str(e)}")


@router.get('/domain/status')
async def get_domain_status(
    request: Request,
    hostname: str | None = None,
    db: Session = Depends(get_db)
):
    """Check DNS CNAME and TLS status for a custom domain.
    Uses DNS over HTTPS (Cloudflare) to avoid extra dependencies.
    """
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Resolve hostname: from query or from saved shop config
    hostname = (hostname or '').strip().lower()
    try:
        shop = db.query(Shop).filter(Shop.uid == uid).first()
        if not shop:
            raise HTTPException(status_code=404, detail="Shop not found")
        if not hostname:
            hostname = (shop.domain or {}).get('hostname') or ''
        if not hostname:
            raise HTTPException(status_code=400, detail="No hostname configured")

        dns_verified = False
        cname_target = None
        cf_url = f"https://cloudflare-dns.com/dns-query?name={hostname}&type=CNAME"
        headers = {"Accept": "application/dns-json"}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(cf_url, headers=headers)
                data = resp.json()
                answers = data.get('Answer') or []
                for ans in answers:
                    if (ans.get('type') == 5) and ans.get('data'):
                        cname_target = (ans['data'] or '').strip('.').lower()
                        if cname_target == 'api.photomark.cloud':
                            dns_verified = True
                            break
        except Exception:
            dns_verified = False

        ssl_status = 'unknown'
        ssl_error = None
        if dns_verified:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    r = await client.head(f"https://{hostname}", follow_redirects=True)
                    if r.status_code < 400:
                        ssl_status = 'active'
                    else:
                        ssl_status = 'pending'
            except Exception as e:
                ssl_error = str(e)
                ssl_status = 'pending'
        else:
            ssl_status = 'blocked'

        shop.domain = {
            "hostname": hostname,
            "dnsTarget": "api.photomark.cloud",
            "dnsVerified": dns_verified,
            "sslStatus": ssl_status,
            "lastChecked": datetime.utcnow().isoformat(),
            "cnameObserved": cname_target,
            "error": ssl_error,
            "enabled": bool((shop.domain or {}).get('enabled') or False)
        }
        shop.updated_at = datetime.utcnow()
        db.commit()

        return {
            "hostname": hostname,
            "dnsVerified": dns_verified,
            "sslStatus": ssl_status,
            "cnameObserved": cname_target,
            "enabled": bool((shop.domain or {}).get('enabled') or False),
            "instructions": {
                "recordType": "CNAME",
                "name": hostname,
                "value": "api.photomark.cloud",
                "ttl": 300
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to check domain status: {str(e)}")

# -------------------------------
# Dynamic checkout link for public shop (no per-product creation in Dodo)
# -------------------------------
from core.config import DODO_ADHOC_PRODUCT_ID, logger  # type: ignore
from utils.dodo import create_checkout_link, create_checkout_session  # type: ignore

@router.get("/checkout/health")
async def checkout_health():
    """
    Quick config health check for Dodo checkout integration.
    - Verifies presence of mandatory vars
    - Lists available per-currency ad-hoc product IDs: DODO_ADHOC_PRODUCT_ID_<CURRENCY>
    """
    import os as _os  # reflect live env at call-time
    from core.config import DODO_API_BASE, DODO_API_KEY, DODO_ADHOC_PRODUCT_ID  # type: ignore

    missing: list[str] = []
    if not (DODO_API_BASE or "").strip():
        missing.append("DODO_API_BASE")
    if not (DODO_API_KEY or "").strip():
        missing.append("DODO_API_KEY")
    # Generic is optional if per-currency IDs are configured
    has_generic = bool((DODO_ADHOC_PRODUCT_ID or "").strip())

    # Discover per-currency overrides present in the env
    currency_map = sorted(
        [
            k.split("DODO_ADHOC_PRODUCT_ID_", 1)[1]
            for k, v in _os.environ.items()
            if k.startswith("DODO_ADHOC_PRODUCT_ID_") and str(v or "").strip()
        ]
    )

    if not has_generic and not currency_map:
        missing.append("DODO_ADHOC_PRODUCT_ID or DODO_ADHOC_PRODUCT_ID_<CURRENCY>")

    details = {
        "api_base": (DODO_API_BASE or "").strip(),
        "api_key_set": bool((DODO_API_KEY or "").strip()),
        "adhoc_product_id_set": has_generic,
        "adhoc_currency_map": currency_map,  # e.g., ["USD","EUR"]
        "how_to_configure": "Set DODO_ADHOC_PRODUCT_ID for a default pay-what-you-want product, or set DODO_ADHOC_PRODUCT_ID_<CURRENCY> per currency (e.g., DODO_ADHOC_PRODUCT_ID_USD).",
    }
    ok = len(missing) == 0
    if not ok:
        logger.warning(f"[shop.checkout.health] Missing config: {missing} | details={details}")
    return JSONResponse({"ok": ok, "missing": missing, "details": details})

def _cents(amount: float) -> int:
    try:
        # Protect against floats and strings; round to nearest cent
        return int(round(float(amount) * 100.0))
    except Exception:
        return 0

@router.post("/checkout/link")
async def create_shop_checkout_link(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db)
):
    """
    Create a hosted Dodo Payments checkout link for a public shop cart
    without creating individual products in Dodo.

    Request JSON:
    {
      "slug": "my-shop",
      "items": [{ "id": "product_id_in_shop", "quantity": 1 }],
      "customer": { "email": "buyer@example.com", "name": "John Doe" },
      "returnUrl": "https://photomark.cloud/#thank-you"  // optional
    }

    Response: { "url": "<redirect-to-dodo-checkout>" }
    """
    # Validate input
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid_payload"}, status_code=400)

    slug = str(payload.get("slug") or "").strip()
    items_in = payload.get("items") or []
    customer = payload.get("customer") or {}
    return_url = str(
        (payload.get("returnUrl") if isinstance(payload, dict) else None)
        or (payload.get("return_url") if isinstance(payload, dict) else None)
        or os.getenv("SHOP_CHECKOUT_RETURN_URL")
        or "https://photomark.cloud/#success"
    )

    if not slug:
        return JSONResponse({"error": "missing_slug"}, status_code=400)
    if not isinstance(items_in, list) or len(items_in) == 0:
        return JSONResponse({"error": "empty_cart"}, status_code=400)
    # Generic check removed here to allow per-currency overrides;
    # validation happens after we determine the cart currency.
    if not DODO_ADHOC_PRODUCT_ID:
        logger.warning("[shop.checkout] DODO_ADHOC_PRODUCT_ID not set; will look for per-currency override (DODO_ADHOC_PRODUCT_ID_<CURRENCY>)")

    # Load shop by slug
    try:
        slug_mapping = db.query(ShopSlug).filter(ShopSlug.slug == slug).first()
        if not slug_mapping:
            return JSONResponse({"error": "shop_not_found"}, status_code=404)
        shop = db.query(Shop).filter(Shop.uid == slug_mapping.uid).first()
        if not shop:
            return JSONResponse({"error": "shop_not_found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": "db_error", "detail": str(e)}, status_code=500)

    # Validate and price items against server data to prevent client tampering
    catalog = {str(p.get("id")): p for p in (shop.products or []) if isinstance(p, dict) and p.get("id")}
    line_items = []
    currency_set: set[str] = set()
    total_cents = 0

    for line in items_in:
        try:
            pid = str(line.get("id") or "").strip()
            qty = int(line.get("quantity") or 1)
            qty = 1 if qty <= 0 else qty
        except Exception:
            return JSONResponse({"error": "invalid_items"}, status_code=400)

        prod = catalog.get(pid)
        if not prod:
            return JSONResponse({"error": "invalid_item", "item_id": pid}, status_code=400)

        price = float(prod.get("price") or 0.0)
        curr = (prod.get("currency") or "USD").strip().upper()
        currency_set.add(curr)
        unit_cents = _cents(price)
        line_total = unit_cents * qty
        total_cents += line_total

        line_items.append({
            "id": pid,
            "title": prod.get("title"),
            "unit_price_cents": unit_cents,
            "quantity": qty,
            "line_total_cents": line_total,
            "currency": curr,
        })

    if total_cents <= 0:
        return JSONResponse({"error": "invalid_total"}, status_code=400)

    if len(currency_set) != 1:
        return JSONResponse({"error": "mixed_currency_not_supported", "currencies": sorted(list(currency_set))}, status_code=400)

    currency = next(iter(currency_set))

    # Resolve ad-hoc product id for this currency: prefer DODO_ADHOC_PRODUCT_ID_<CURRENCY>, fallback to generic
    ADHOC_ID = (os.getenv(f"DODO_ADHOC_PRODUCT_ID_{currency}", "") or "").strip() or DODO_ADHOC_PRODUCT_ID
    if not ADHOC_ID:
        logger.warning(f"[shop.checkout] No ad-hoc product configured for currency={currency}")
        return JSONResponse(
            {
                "error": "adhoc_product_not_configured",
                "missing_env": f"DODO_ADHOC_PRODUCT_ID_{currency} or DODO_ADHOC_PRODUCT_ID",
                "currency": currency,
                "how_to_fix": f"Create a single one-time product in Dodo with pay_what_you_want enabled for {currency}, then set DODO_ADHOC_PRODUCT_ID_{currency}. "
                              f"Alternatively set a generic DODO_ADHOC_PRODUCT_ID if your ad-hoc product currency matches the cart currency.",
                "health_check": "/api/shop/checkout/health"
            },
            status_code=500
        )

    # Prepare payloads for Dodo create-checkout flow (using a single pay-what-you-want product)
    owner_uid = shop.owner_uid
    shop_uid = shop.uid
    meta = {
        "shop_slug": slug,
        "shop_uid": shop_uid,
        "owner_uid": owner_uid,
        "currency": currency,
        "cart_total_cents": total_cents,
        "cart_items": line_items,
    }
    qp = {"shop_slug": slug, "owner_uid": owner_uid}

    # Business/brand identifiers for provider compatibility
    business_id = (os.getenv("DODO_BUSINESS_ID") or "").strip()
    brand_id = (os.getenv("DODO_BRAND_ID") or "").strip()
    common_top = {**({"business_id": business_id} if business_id else {}), **({"brand_id": brand_id} if brand_id else {})}

    # Reference identifiers to help reconcile in webhooks
    ref_fields = {
        "client_reference_id": f"{owner_uid}:{slug}",
        "reference_id": f"{owner_uid}:{slug}",
        "external_id": f"{owner_uid}:{slug}",
    }

    # Build primary payload (unified shape)
    # Note: Do NOT include "payment_link" here; some checkout-session endpoints reject unknown fields.
    base_payload = {
        **common_top,
        **ref_fields,
        "metadata": meta,
        "query_params": qp,
        "query": qp,
        "params": qp,
        "product_cart": [
            {
                "product_id": ADHOC_ID,
                "quantity": 1,
                "amount": int(total_cents),  # lowest denomination
            }
        ],
        "billing_currency": currency,
        "return_url": return_url,
        "cancel_url": return_url,
        "allowed_payment_method_types": ["credit", "debit", "apple_pay", "google_pay"],
        "show_saved_payment_methods": True,
    }

    # Add customer info if provided
    cust_email = str(customer.get("email") or "").strip() if isinstance(customer, dict) else ""
    cust_name = str(customer.get("name") or "").strip() if isinstance(customer, dict) else ""
    if cust_email or cust_name:
        base_payload["customer"] = {**({"email": cust_email} if cust_email else {}), **({"name": cust_name} if cust_name else {})}
        if cust_email:
            base_payload["email"] = cust_email
            base_payload["customer_email"] = cust_email

    # Minimal session payload per provider guidance
    session_payload = {
        **common_top,
        "product_cart": [
            {
                "product_id": ADHOC_ID,
                "quantity": 1,
                "amount": int(total_cents),
            }
        ],
        "return_url": return_url,
    }
    session_data, error = await create_checkout_session(session_payload)
    if session_data and isinstance(session_data, dict):
        link = (
            session_data.get("checkout_url")
            or session_data.get("session_url")
            or session_data.get("url")
        )
        if isinstance(link, str) and link:
            try:
                from utils.storage import write_json_key  # type: ignore
                code = link.rsplit("/", 1)[-1]
                payload = {
                    "shop_slug": slug,
                    "shop_uid": shop_uid,
                    "owner_uid": owner_uid,
                    "currency": currency,
                    "cart_total_cents": total_cents,
                    "cart_items": line_items,
                    "email": cust_email,
                    "name": cust_name,
                    "session_id": session_data.get("session_id") or session_data.get("id"),
                }
                write_json_key(
                    f"shops/cache/links/{code}.json",
                    payload,
                )
                sid = payload.get("session_id")
                if isinstance(sid, str) and sid:
                    write_json_key(
                        f"shops/cache/sessions/{sid}.json",
                        payload,
                    )
            except Exception:
                pass
            return {"url": link, "session_id": session_data.get("session_id") or session_data.get("id")}

    logger.warning(f"[shop.checkout] failed to create checkout session: {error}")
    return JSONResponse({"error": "session_creation_failed", "details": error}, status_code=502)
@router.get('/upload')
async def upload_info():
    return JSONResponse({"error": "Use POST multipart/form-data to /api/shop/upload"}, status_code=405)
