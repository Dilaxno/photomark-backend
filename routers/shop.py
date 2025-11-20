from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse
from core.auth import get_current_user
from typing import Optional
import boto3
import os
import uuid
from datetime import datetime

router = APIRouter()

# R2 Configuration
R2_ACCOUNT_ID = os.getenv('R2_ACCOUNT_ID')
R2_ACCESS_KEY_ID = os.getenv('R2_ACCESS_KEY_ID')
R2_SECRET_ACCESS_KEY = os.getenv('R2_SECRET_ACCESS_KEY')
R2_BUCKET_NAME = os.getenv('R2_BUCKET_NAME', 'photomark-shops')

# Initialize R2 client
s3_client = boto3.client(
    's3',
    endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name='auto'
)

@router.post('/api/shop/upload')
async def upload_shop_asset(
    file: UploadFile = File(...),
    type: str = Form(...),  # 'logo', 'pfp', or 'banner'
    shop_id: str = Form(...),
    user=Depends(get_current_user)
):
    """
    Upload shop assets (logo, pfp, banner) to R2 storage
    """
    try:
        # Validate file type
        allowed_types = ['image/jpeg', 'image/jpg', 'image/png', 'image/gif', 'image/webp']
        if file.content_type not in allowed_types:
            raise HTTPException(status_code=400, detail='Invalid file type. Only images allowed.')
        
        # Validate file size (max 10MB)
        contents = await file.read()
        if len(contents) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail='File too large. Maximum size is 10MB.')
        
        # Generate unique filename
        file_extension = file.filename.split('.')[-1] if '.' in file.filename else 'jpg'
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        unique_id = str(uuid.uuid4())[:8]
        filename = f'shops/{shop_id}/{type}/{timestamp}_{unique_id}.{file_extension}'
        
        # Upload to R2
        s3_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=filename,
            Body=contents,
            ContentType=file.content_type,
            CacheControl='public, max-age=31536000'  # Cache for 1 year
        )
        
        # Generate public URL
        # Note: You need to configure R2 custom domain or public access
        r2_public_domain = os.getenv('R2_PUBLIC_DOMAIN', f'{R2_BUCKET_NAME}.r2.dev')
        url = f'https://{r2_public_domain}/{filename}'
        
        return JSONResponse({
            'success': True,
            'url': url,
            'filename': filename,
            'type': type
        })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f'Error uploading to R2: {str(e)}')
        raise HTTPException(status_code=500, detail=f'Upload failed: {str(e)}')


@router.delete('/api/shop/upload/{filename:path}')
async def delete_shop_asset(
    filename: str,
    user=Depends(get_current_user)
):
    """
    Delete shop asset from R2 storage
    """
    try:
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


@router.get('/api/shop/{shop_id}/settings')
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


@router.post('/api/shop/{shop_id}/settings')
async def save_shop_settings(
    shop_id: str,
    settings: dict,
    user=Depends(get_current_user)
):
    """
    Save shop settings to database
    """
    try:
        # TODO: Save to database (Firestore or PostgreSQL)
        # For now, just acknowledge
        return JSONResponse({
            'success': True,
            'message': 'Settings saved'
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
