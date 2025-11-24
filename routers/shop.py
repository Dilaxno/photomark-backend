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
import boto3
from botocore.client import Config

router = APIRouter(prefix="/api/shop", tags=["shop"])

# Allowed file types
IMAGE_TYPES = ['image/jpeg', 'image/jpg', 'image/png', 'image/gif', 'image/webp']
VIDEO_TYPES = ['video/mp4', 'video/webm', 'video/quicktime', 'video/x-msvideo']

# File size limits
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_VIDEO_SIZE = 100 * 1024 * 1024  # 100MB

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
    Returns a base64 data URL for immediate use.
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

        # Try to upload to Cloudflare R2 (S3-compatible)
        r2_url = None
        try:
            R2_ACCOUNT_ID = os.getenv('R2_ACCOUNT_ID', '').strip()
            R2_ACCESS_KEY_ID = os.getenv('R2_ACCESS_KEY_ID', '').strip()
            R2_SECRET_ACCESS_KEY = os.getenv('R2_SECRET_ACCESS_KEY', '').strip()
            R2_BUCKET = os.getenv('R2_BUCKET', '').strip()
            R2_PUBLIC_BASE_URL = os.getenv('R2_PUBLIC_BASE_URL', '').rstrip('/') if os.getenv('R2_PUBLIC_BASE_URL') else ''

            if R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET and R2_PUBLIC_BASE_URL:
                endpoint_url = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
                s3 = boto3.client(
                    's3',
                    aws_access_key_id=R2_ACCESS_KEY_ID,
                    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
                    endpoint_url=endpoint_url,
                    config=Config(signature_version='s3v4'),
                    region_name='auto'
                )

                object_key = f"shops/{shop_id}/{filename}"
                s3.put_object(
                    Bucket=R2_BUCKET,
                    Key=object_key,
                    Body=contents,
                    ContentType=file.content_type,
                )
                r2_url = f"{R2_PUBLIC_BASE_URL}/{object_key}"
                print(f"[SHOP UPLOAD] Uploaded to R2: {r2_url}")
            else:
                print("[SHOP UPLOAD] R2 env vars missing; skipping R2 upload.")
        except Exception as r2_err:
            print(f"[SHOP UPLOAD] WARNING: R2 upload failed: {r2_err}")

        # Fallback: save the file to static directory for persistent URL
        # Path: static/shops/<shop_id>/<filename>
        static_url = None
        if not r2_url:
            try:
                base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
                static_dir = os.path.join(base_dir, 'static')
                target_dir = os.path.join(static_dir, 'shops', shop_id)
                os.makedirs(target_dir, exist_ok=True)

                file_path = os.path.join(target_dir, filename)
                with open(file_path, 'wb') as f:
                    f.write(contents)

                static_url = f"/static/shops/{shop_id}/{filename}"
                static_base = os.getenv('STATIC_PUBLIC_BASE_URL', '').rstrip('/') if os.getenv('STATIC_PUBLIC_BASE_URL') else ''
                if static_base:
                    static_url = f"{static_base}{static_url}"
            except Exception as write_err:
                print(f"[SHOP UPLOAD] WARNING: Failed to persist file to static dir: {write_err}")

        # Generate base64 data URL for immediate preview (do not store in DB)
        base64_data = base64.b64encode(contents).decode('utf-8')
        data_url = f"data:{file.content_type};base64,{base64_data}"

        print(f"[SHOP UPLOAD] Success - Generated: {filename} (r2: {bool(r2_url)}, static: {bool(static_url)})")

        return JSONResponse({
            'success': True,
            'url': r2_url or static_url or data_url,  # prefer R2 URL to keep Firestore docs small
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
