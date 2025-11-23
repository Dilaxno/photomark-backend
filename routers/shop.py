from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse
from core.auth import get_uid_from_request
from typing import Optional
import boto3
import os
import uuid
from datetime import datetime

router = APIRouter(prefix="/api/shop", tags=["shop"])

# R2 Configuration
R2_ACCOUNT_ID = os.getenv('R2_ACCOUNT_ID')
R2_ACCESS_KEY_ID = os.getenv('R2_ACCESS_KEY_ID')
R2_SECRET_ACCESS_KEY = os.getenv('R2_SECRET_ACCESS_KEY')
R2_BUCKET_NAME = os.getenv('R2_BUCKET_NAME', 'photomark-shops')

# Check if R2 is configured
R2_CONFIGURED = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY])

if not R2_CONFIGURED:
    print('WARNING: R2 storage not configured. Shop uploads will fail.')
    print('Please set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY environment variables.')
    s3_client = None
else:
    # Initialize R2 client
    try:
        s3_client = boto3.client(
            's3',
            endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name='auto'
        )
        print(f'R2 client initialized successfully for bucket: {R2_BUCKET_NAME}')
    except Exception as e:
        print(f'ERROR: Failed to initialize R2 client: {str(e)}')
        s3_client = None
        R2_CONFIGURED = False

@router.post('/upload')
async def upload_shop_asset(
    request: Request,
    file: UploadFile = File(...),
    type: str = Form(...),  # 'logo', 'pfp', or 'banner'
    shop_id: str = Form(...)
):
    """
    Upload shop assets (logo, pfp, banner) to R2 storage
    """
    try:
        # Check if R2 is configured
        if not R2_CONFIGURED or s3_client is None:
            print('ERROR: R2 storage not configured')
            raise HTTPException(
                status_code=503, 
                detail='File upload service not configured. Please contact administrator.'
            )
        
        # Verify user is authenticated
        uid = get_uid_from_request(request)
        if not uid:
            print(f'ERROR: Unauthorized upload attempt for shop {shop_id}')
            raise HTTPException(status_code=401, detail='Unauthorized')
        # Log upload attempt
        print(f'Upload attempt - User: {uid}, Shop: {shop_id}, Type: {type}, File: {file.filename}, ContentType: {file.content_type}')
        
        # Validate file type based on upload type
        if type in ['logo', 'pfp', 'banner', 'product_image']:
            allowed_types = ['image/jpeg', 'image/jpg', 'image/png', 'image/gif', 'image/webp']
            if file.content_type not in allowed_types:
                print(f'ERROR: Invalid file type {file.content_type} for {type}')
                raise HTTPException(status_code=400, detail=f'Invalid file type. Only images allowed. Got: {file.content_type}')
        elif type == 'product_video':
            allowed_types = ['video/mp4', 'video/webm', 'video/quicktime', 'video/x-msvideo']
            if file.content_type not in allowed_types:
                print(f'ERROR: Invalid video type {file.content_type}')
                raise HTTPException(status_code=400, detail=f'Invalid video type. Only MP4, WebM, MOV, AVI allowed. Got: {file.content_type}')
        
        # Validate file size
        contents = await file.read()
        file_size = len(contents)
        max_size = 100 * 1024 * 1024 if type == 'product_video' else 10 * 1024 * 1024  # 100MB for video, 10MB for images
        
        print(f'File size: {file_size / (1024 * 1024):.2f}MB')
        
        if file_size > max_size:
            max_size_mb = max_size / (1024 * 1024)
            print(f'ERROR: File too large: {file_size / (1024 * 1024):.2f}MB > {max_size_mb}MB')
            raise HTTPException(status_code=400, detail=f'File too large. Maximum size is {max_size_mb}MB.')
        
        # Generate unique filename
        file_extension = file.filename.split('.')[-1] if '.' in file.filename else 'jpg'
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        unique_id = str(uuid.uuid4())[:8]
        filename = f'shops/{shop_id}/{type}/{timestamp}_{unique_id}.{file_extension}'
        
        print(f'Uploading to R2: {filename}')
        
        # Upload to R2
        try:
            s3_client.put_object(
                Bucket=R2_BUCKET_NAME,
                Key=filename,
                Body=contents,
                ContentType=file.content_type,
                CacheControl='public, max-age=31536000'  # Cache for 1 year
            )
            print(f'Successfully uploaded to R2: {filename}')
        except Exception as e:
            print(f'ERROR: R2 upload failed: {str(e)}')
            print(f'Bucket: {R2_BUCKET_NAME}, Key: {filename}')
            raise HTTPException(status_code=500, detail=f'Failed to upload to storage: {str(e)}')
        
        # Generate public URL
        # Note: You need to configure R2 custom domain or public access
        r2_public_domain = os.getenv('R2_PUBLIC_DOMAIN', f'{R2_BUCKET_NAME}.r2.dev')
        url = f'https://{r2_public_domain}/{filename}'
        
        print(f'Upload successful. URL: {url}')
        
        return JSONResponse({
            'success': True,
            'url': url,
            'filename': filename,
            'type': type
        })
        
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f'UNEXPECTED ERROR uploading to R2: {str(e)}')
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f'Upload failed: {str(e)}')


@router.delete('/upload/{filename:path}')
async def delete_shop_asset(
    request: Request,
    filename: str
):
    """
    Delete shop asset from R2 storage
    """
    try:
        # Verify user is authenticated
        uid = get_uid_from_request(request)
        if not uid:
            raise HTTPException(status_code=401, detail='Unauthorized')
        # Delete from R2
        s3_client.delete_object(
            Bucket=R2_BUCKET_NAME,
            Key=filename
        )
        
        return JSONResponse({
            'success': True,
            'message': 'File deleted successfully'
        })
        
    except Exception as e:
        print(f'Error deleting from R2: {str(e)}')
        raise HTTPException(status_code=500, detail=f'Delete failed: {str(e)}')


@router.get('/{shop_id}/settings')
async def get_shop_settings(shop_id: str):
    """
    Get shop settings (public endpoint)
    """
    # TODO: Fetch from database
    # For now, return empty as frontend uses localStorage
    return JSONResponse({
        'success': True,
        'settings': {}
    })


@router.post('/{shop_id}/settings')
async def save_shop_settings(
    request: Request,
    shop_id: str,
    settings: dict
):
    """
    Save shop settings to database
    """
    try:
        # Verify user is authenticated
        uid = get_uid_from_request(request)
        if not uid:
            raise HTTPException(status_code=401, detail='Unauthorized')
        # TODO: Save to database (Firestore or PostgreSQL)
        # For now, just acknowledge
        return JSONResponse({
            'success': True,
            'message': 'Settings saved'
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
