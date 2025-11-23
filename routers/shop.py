from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Request, Depends
from fastapi.responses import JSONResponse
from core.auth import get_uid_from_request
import os
import uuid
import base64
from datetime import datetime
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
