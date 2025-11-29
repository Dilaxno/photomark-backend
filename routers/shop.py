from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Request, Depends, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List, Literal, Dict, Any
from core.auth import get_uid_from_request, resolve_workspace_uid
from core.database import get_db
from models.shop import Shop, ShopSlug
from models.shop_sales import ShopSale
from models.shop_traffic import ShopTraffic
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
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

# --- Response Schemas (Pydantic) ---
class ProductSchema(BaseModel):
    id: str
    title: str
    description: str
    price: float
    currency: str
    images: List[str] = []
    videoUrl: Optional[str] = None
    digitalFile: Optional[str] = None
    category: Optional[str] = None
    tags: List[str] = []
    featured: Optional[bool] = None
    active: Optional[bool] = None

class ShopThemeSchema(BaseModel):
    primaryColor: Optional[str] = None
    secondaryColor: Optional[str] = None
    accentColor: Optional[str] = None
    backgroundColor: Optional[str] = None
    textColor: Optional[str] = None
    fontFamily: Optional[str] = None
    logoUrl: Optional[str] = None
    bannerUrl: Optional[str] = None
    customFontUrl: Optional[str] = None

class ShopSettingsSchema(BaseModel):
    name: str
    slug: str
    description: Optional[str] = None
    ownerUid: str
    ownerName: Optional[str] = None
    theme: ShopThemeSchema
    domain: Dict[str, Any] = {}
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None

class ShopDataSchema(BaseModel):
    settings: ShopSettingsSchema
    products: List[ProductSchema] = []

class DomainStatusSchema(BaseModel):
    hostname: str
    dnsVerified: bool
    sslStatus: str
    cnameObserved: Optional[str] = None
    enabled: Optional[bool] = None
    instructions: Dict[str, Any]

class ShopSaleSchema(BaseModel):
    id: str
    payment_id: Optional[str] = None
    owner_uid: str
    shop_uid: Optional[str] = None
    slug: Optional[str] = None
    currency: str
    amount_cents: int
    items: List[Any] = []
    metadata: Dict[str, Any] = {}
    delivered: bool
    customer_email: Optional[str] = None
    created_at: Optional[str] = None

class SalesResponseSchema(BaseModel):
    sales: List[ShopSaleSchema]
    count: int

class TrafficStatSchema(BaseModel):
    date: str
    views: int

class TrafficResponseSchema(BaseModel):
    stats: List[TrafficStatSchema]
    total: int


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


@router.get('/uid/{uid}', response_model=ShopDataSchema)
async def get_shop_by_uid(uid: str, db: Session = Depends(get_db)):
    try:
        key = (uid or "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="Invalid uid")
        shop = db.query(Shop).filter(Shop.uid == key).first()
        if not shop:
            shop = db.query(Shop).filter(Shop.owner_uid == key).first()
        if not shop:
            m = db.query(ShopSlug).filter(ShopSlug.slug == key).first()
            if m:
                shop = db.query(Shop).filter(Shop.uid == m.uid).first()
        if not shop:
            raise HTTPException(status_code=404, detail="Shop not found")
        data = shop.to_dict()
        try:
            s = data.get("settings") or {}
            t = s.get("theme") or {}
            for k in ("logoUrl", "bannerUrl", "customFontUrl"):
                v = t.get(k)
                if isinstance(v, str):
                    t[k] = v.strip().strip('`')
            s["theme"] = t
            data["settings"] = s
            prods = data.get("products") or []
            fixed = []
            for p in prods:
                if isinstance(p, dict):
                    imgs = p.get("images") or []
                    if isinstance(imgs, list):
                        imgs = [((i.strip().strip('`')) if isinstance(i, str) else i) for i in imgs]
                        p["images"] = imgs
                    for k in ("videoUrl", "digitalFile"):
                        v = p.get(k)
                        if isinstance(v, str):
                            p[k] = v.strip().strip('`')
                    fixed.append(p)
            data["products"] = fixed
        except Exception:
            pass
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get shop: {str(e)}")


@router.get('/slug/{slug}', response_model=ShopDataSchema)
async def get_shop_by_slug(slug: str, db: Session = Depends(get_db)):
    """Get shop data by slug (for public viewing)"""
    try:
        key = (slug or "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="Invalid slug")

        # Preferred: look up UID from slug mapping
        slug_mapping = db.query(ShopSlug).filter(ShopSlug.slug == key).first()
        shop = None
        if slug_mapping:
            shop = db.query(Shop).filter(Shop.uid == slug_mapping.uid).first()

        # Fallback: query shops table by slug directly (for legacy/migration cases)
        if not shop:
            shop = db.query(Shop).filter(Shop.slug == key).first()

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

@router.get('/subscription/{sub_id}', response_model=ShopDataSchema)
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
    uid, _ = resolve_workspace_uid(request)
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


@router.get('/payout/settings')
async def get_payout_settings(
    request: Request,
    db: Session = Depends(get_db)
):
    uid, _ = resolve_workspace_uid(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        shop = db.query(Shop).filter(Shop.uid == uid).first()
        if not shop:
            shop = db.query(Shop).filter(Shop.owner_uid == uid).first()
        if not shop:
            raise HTTPException(status_code=404, detail="Shop not found")
        dom = shop.domain or {}
        return (dom.get('payout') or {})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load payout settings: {str(e)}")


@router.post('/payout/settings')
async def save_payout_settings(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db)
):
    uid, _ = resolve_workspace_uid(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        shop = db.query(Shop).filter(Shop.uid == uid).first()
        if not shop:
            shop = db.query(Shop).filter(Shop.owner_uid == uid).first()
        if not shop:
            raise HTTPException(status_code=404, detail="Shop not found")
        dom = shop.domain or {}
        dom['payout'] = payload or {}
        shop.domain = dom
        shop.updated_at = datetime.utcnow()
        db.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save payout settings: {str(e)}")


@router.get('/payout/next')
async def get_next_payout(
    request: Request,
    db: Session = Depends(get_db)
):
    uid, _ = resolve_workspace_uid(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        shop = db.query(Shop).filter(Shop.uid == uid).first()
        if not shop:
            shop = db.query(Shop).filter(Shop.owner_uid == uid).first()
        if not shop:
            raise HTTPException(status_code=404, detail="Shop not found")
        dom = shop.domain or {}
        sched = dom.get('payout_schedule') or {}
        return sched
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load payout schedule: {str(e)}")


@router.post('/payout/next')
async def set_next_payout(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db)
):
    uid, _ = resolve_workspace_uid(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    next_at = str((payload.get('nextPayoutAt') or '')).strip()
    cadence = str((payload.get('cadence') or 'biweekly')).strip().lower()
    weekday = str((payload.get('weekday') or 'friday')).strip().lower()
    try:
        shop = db.query(Shop).filter(Shop.uid == uid).first()
        if not shop:
            shop = db.query(Shop).filter(Shop.owner_uid == uid).first()
        if not shop:
            raise HTTPException(status_code=404, detail="Shop not found")
        dom = shop.domain or {}
        dom['payout_schedule'] = {"nextPayoutAt": next_at, "cadence": cadence, "weekday": weekday}
        shop.domain = dom
        shop.updated_at = datetime.utcnow()
        db.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to set payout schedule: {str(e)}")


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


@router.get('/domain/status', response_model=DomainStatusSchema)
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

@router.get('/sales', response_model=SalesResponseSchema)
async def get_shop_sales(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    slug: str = "",
    owner_uid: str = "",
    db: Session = Depends(get_db)
):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    target_uid = (owner_uid or eff_uid).strip()
    if owner_uid and owner_uid != eff_uid:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        q = db.query(ShopSale).filter(or_(ShopSale.owner_uid == target_uid, ShopSale.shop_uid == target_uid))
        if slug:
            q = q.filter(ShopSale.slug == slug)
        rows = (
            q.order_by(ShopSale.created_at.desc())
             .offset(max(0, int(offset)))
             .limit(max(1, min(int(limit), 200)))
             .all()
        )
        out = []
        for s in rows:
            out.append({
                "id": s.id,
                "payment_id": s.payment_id,
                "owner_uid": s.owner_uid,
                "shop_uid": s.shop_uid,
                "slug": s.slug,
                "currency": s.currency,
                "amount_cents": s.amount_cents,
                "items": s.items,
                "metadata": s.sale_metadata,
                "delivered": bool(s.delivered),
                "customer_email": s.customer_email,
                "customer_name": getattr(s, "customer_name", None),
                "customer_city": getattr(s, "customer_city", None),
                "customer_country": getattr(s, "customer_country", None),
                "created_at": s.created_at.isoformat() if s.created_at else None,
            })
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        }
        return JSONResponse({"sales": out, "count": len(out)}, headers=headers)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch sales: {str(e)}")

@router.post('/traffic/visit')
async def track_shop_visit(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db)
):
    try:
        slug = str(payload.get("slug") or "").strip()
        path = str(payload.get("path") or "")[:512]
        ref = str(payload.get("referrer") or "")
        ua = request.headers.get("user-agent") or ""
        ip = getattr(getattr(request, 'client', None), 'host', None) or request.headers.get("x-forwarded-for") or ""
        device = str(payload.get("device") or "")
        browser = str(payload.get("browser") or "")
        os_name = str(payload.get("os") or "")

        mapping = None
        owner_uid = None
        shop_uid = None
        if slug:
            mapping = db.query(ShopSlug).filter(ShopSlug.slug == slug).first()
            if mapping:
                shop_uid = mapping.uid
                shop = db.query(Shop).filter(Shop.uid == shop_uid).first()
                owner_uid = shop.owner_uid if shop else None

        if not owner_uid:
            eff_uid, _ = resolve_workspace_uid(request)
            owner_uid = eff_uid

        if not owner_uid:
            return JSONResponse({"ok": False}, status_code=200)

        from uuid import uuid4
        rec = ShopTraffic(
            id=str(uuid4()).replace('-', '')[:24],
            owner_uid=owner_uid,
            shop_uid=shop_uid,
            slug=slug or None,
            path=path or None,
            referrer=ref or None,
            ip=str(ip)[:64] if ip else None,
            user_agent=ua or None,
            device=device or None,
            browser=browser or None,
            os=os_name or None,
        )
        db.add(rec)
        db.commit()
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        }
        return JSONResponse({"ok": True}, headers=headers)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to track visit: {str(e)}")

@router.get('/traffic/stats', response_model=TrafficResponseSchema)
async def get_traffic_stats(
    request: Request,
    days: int = 30,
    slug: str = "",
    db: Session = Depends(get_db)
):
    eff_uid, _ = resolve_workspace_uid(request)
    if not eff_uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        q = db.query(
            func.date(ShopTraffic.created_at).label('d'),
            func.count(ShopTraffic.id).label('views')
        ).filter(ShopTraffic.owner_uid == eff_uid)
        if slug:
            q = q.filter(ShopTraffic.slug == slug)
        from datetime import datetime, timedelta
        since = datetime.utcnow() - timedelta(days=max(1, min(365, int(days))))
        q = q.filter(ShopTraffic.created_at >= since).group_by('d').order_by('d')
        rows = q.all()
        stats = [{"date": r[0].isoformat(), "views": int(r[1])} for r in rows]
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        }
        return JSONResponse({"stats": stats, "total": sum(s['views'] for s in stats)}, headers=headers)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch traffic: {str(e)}")

@router.options('/traffic/visit')
async def traffic_visit_options():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("", status_code=204, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    })

@router.options('/traffic/stats')
async def traffic_stats_options():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("", status_code=204, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    })
@router.get('/sales/owner/{owner_uid}', response_model=SalesResponseSchema)
async def get_shop_sales_by_owner(
    request: Request,
    owner_uid: str,
    limit: int = 50,
    offset: int = 0,
    slug: str = "",
    db: Session = Depends(get_db)
):
    eff_uid, _ = resolve_workspace_uid(request)
    if not eff_uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if owner_uid != eff_uid:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        q = db.query(ShopSale).filter(or_(ShopSale.owner_uid == owner_uid, ShopSale.shop_uid == owner_uid))
        if slug:
            q = q.filter(ShopSale.slug == slug)
        rows = (
            q.order_by(ShopSale.created_at.desc())
             .offset(max(0, int(offset)))
             .limit(max(1, min(int(limit), 200)))
             .all()
        )
        out = []
        for s in rows:
            out.append({
                "id": s.id,
                "payment_id": s.payment_id,
                "owner_uid": s.owner_uid,
                "shop_uid": s.shop_uid,
                "slug": s.slug,
                "currency": s.currency,
                "amount_cents": s.amount_cents,
                "items": s.items,
                "metadata": s.sale_metadata,
                "delivered": bool(s.delivered),
                "customer_email": s.customer_email,
                "customer_name": getattr(s, "customer_name", None),
                "customer_city": getattr(s, "customer_city", None),
                "customer_country": getattr(s, "customer_country", None),
                "created_at": s.created_at.isoformat() if s.created_at else None,
            })
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        }
        return JSONResponse({"sales": out, "count": len(out)}, headers=headers)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch sales: {str(e)}")

@router.options('/sales')
async def sales_options():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("", status_code=204, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    })

@router.options('/sales/owner/{owner_uid}')
async def sales_owner_options(owner_uid: str):
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("", status_code=204, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    })

@router.post('/sales/mark-delivered')
async def mark_all_sales_delivered(
    request: Request,
    db: Session = Depends(get_db)
):
    """Mark all sales as delivered (for digital products that are instantly delivered)"""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        # Update all sales for this owner to delivered=True
        updated = db.query(ShopSale).filter(
            or_(ShopSale.owner_uid == eff_uid, ShopSale.shop_uid == eff_uid)
        ).update({"delivered": True}, synchronize_session=False)
        
        db.commit()
        
        return JSONResponse({
            "ok": True,
            "updated_count": updated,
            "message": f"Marked {updated} sales as delivered"
        })
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update sales: {str(e)}")

@router.get('/upload')
async def upload_info():
    return JSONResponse({"error": "Use POST multipart/form-data to /api/shop/upload"}, status_code=405)

# ===== Discounts (Dodo Payments) endpoints =====
from core.config import DODO_API_BASE
from utils.dodo import build_headers_list
import asyncio

def _dodo_base_url() -> str:
    base_in = (DODO_API_BASE or "").strip()
    low = base_in.lower()
    # Follow Sentra/Dodo guidance: prefer test/live bases; never use api.dodopayments.com
    if (not base_in) or ("example" in low) or ("api.dodopayments.com" in low) or ("api.dodo-payments" in low):
        return "https://test.dodopayments.com"
    return base_in.rstrip("/")

def _pick_headers() -> dict:
    # Use Authorization Bearer variant first; fall back variants exist in list
    variants = build_headers_list()
    return variants[0] if variants else {}

@router.get("/discounts")
async def list_discounts(
    request: Request,
    page_number: int = 0,
    page_size: int = 10,
):
    """
    List discounts from Dodo Payments for the authenticated owner.

    Security:
    - Owner must be authenticated; API key is kept server-side.
    """
    uid, _ = resolve_workspace_uid(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    # basic pagination clamping
    page_number = max(0, int(page_number))
    page_size = max(1, min(100, int(page_size)))
    base = _dodo_base_url()
    url = f"{base}/discounts?page_number={page_number}&page_size={page_size}"
    headers = _pick_headers()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code < 300:
                return resp.json()
            detail = (resp.text or "")[:2000]
            raise HTTPException(status_code=resp.status_code, detail=f"Dodo list discounts failed: {detail}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Dodo list discounts error: {e}")

class CreateDiscountPayload(BaseModel):
    # percentage only (basis points), e.g. 540 = 5.40%
    amount_bp: int
    code: Optional[str] = None
    name: Optional[str] = None
    expires_at: Optional[str] = None  # ISO datetime string
    usage_limit: Optional[int] = None
    restricted_to: Optional[List[str]] = None  # product_ids scope

@router.post("/discounts")
async def create_discount(
    request: Request,
    payload: CreateDiscountPayload,
):
    """
    Create a percentage discount in Dodo Payments.

    Notes:
    - amount_bp is sent as 'amount' in basis points to Dodo with type='percentage'
    - If code omitted, Dodo generates a 16-char uppercase code
    """
    uid, _ = resolve_workspace_uid(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")

    base = _dodo_base_url()
    url = f"{base}/discounts"
    headers = _pick_headers()

    # Build Dodo payload
    body: dict = {
        "amount": int(payload.amount_bp),
        "type": "percentage",
    }
    if payload.code:
        body["code"] = str(payload.code).strip()
    if payload.name:
        body["name"] = str(payload.name).strip()
    if payload.expires_at:
        body["expires_at"] = str(payload.expires_at).strip()
    if isinstance(payload.usage_limit, int) and payload.usage_limit > 0:
        body["usage_limit"] = payload.usage_limit
    if isinstance(payload.restricted_to, list):
        body["restricted_to"] = [str(x) for x in payload.restricted_to if isinstance(x, str)]

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, json=body)
            if resp.status_code < 300:
                return resp.json()
            detail = (resp.text or "")[:2000]
            raise HTTPException(status_code=resp.status_code, detail=f"Dodo create discount failed: {detail}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Dodo create discount error: {e}")

@router.delete("/discounts/{discount_id}")
async def delete_discount(
    request: Request,
    discount_id: str,
):
    """
    Delete a discount in Dodo Payments by id.
    """
    uid, _ = resolve_workspace_uid(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    did = (discount_id or "").strip()
    if not did:
        raise HTTPException(status_code=400, detail="Invalid discount_id")

    base = _dodo_base_url()
    url = f"{base}/discounts/{did}"
    headers = _pick_headers()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.delete(url, headers=headers)
            if resp.status_code < 300:
                return {"ok": True}
            detail = (resp.text or "")[:2000]
            raise HTTPException(status_code=resp.status_code, detail=f"Dodo delete discount failed: {detail}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Dodo delete discount error: {e}")
