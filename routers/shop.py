from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Request, Depends
from fastapi.responses import JSONResponse
from core.auth import get_uid_from_request
import os
import uuid
import base64
from datetime import datetime

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

        # Save the file to static directory for persistent URL
        # Path: static/shops/<shop_id>/<filename>
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            static_dir = os.path.join(base_dir, 'static')
            target_dir = os.path.join(static_dir, 'shops', shop_id)
            os.makedirs(target_dir, exist_ok=True)

            file_path = os.path.join(target_dir, filename)
            with open(file_path, 'wb') as f:
                f.write(contents)

            static_url = f"/static/shops/{shop_id}/{filename}"
        except Exception as write_err:
            print(f"[SHOP UPLOAD] WARNING: Failed to persist file to static dir: {write_err}")
            static_url = None

        # Generate base64 data URL for immediate preview (do not store in DB)
        base64_data = base64.b64encode(contents).decode('utf-8')
        data_url = f"data:{file.content_type};base64,{base64_data}"

        print(f"[SHOP UPLOAD] Success - Generated: {filename} (static: {bool(static_url)})")

        return JSONResponse({
            'success': True,
            'url': static_url or data_url,  # prefer static URL to keep Firestore docs small
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
