#!/usr/bin/env python3
"""
Generate missing thumbnails for existing images in R2/B2 storage.
Run this script to backfill thumbnails for images uploaded before thumbnail generation was added.

Usage:
    python -m scripts.generate_missing_thumbnails [--dry-run] [--limit N] [--user UID]
"""
import os
import sys
import argparse
from datetime import datetime

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import s3, R2_BUCKET, s3_backup, BACKUP_BUCKET, logger
from utils.thumbnails import generate_thumbnail, get_thumbnail_key, THUMB_SMALL
from utils.storage import read_bytes_key, backup_read_bytes_key


# Prefixes to scan for images
SCAN_PREFIXES = [
    "users/{uid}/watermarked/",
    "users/{uid}/external/",
    "users/{uid}/vaults/",
    "users/{uid}/portfolio/",
    "users/{uid}/gallery/",
    "users/{uid}/photos/",
]

# Image extensions to process
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.tif', '.tiff'}


def is_image_key(key: str) -> bool:
    """Check if a key is an image file."""
    lower = key.lower()
    return any(lower.endswith(ext) for ext in IMAGE_EXTENSIONS)


def has_thumbnail(bucket, key: str) -> bool:
    """Check if a thumbnail already exists for this key."""
    thumb_key = get_thumbnail_key(key, 'small')
    try:
        bucket.Object(thumb_key).load()
        return True
    except Exception:
        return False


def generate_and_upload_thumbnail(bucket, key: str, read_func, dry_run: bool = False) -> bool:
    """Generate and upload a thumbnail for the given key."""
    try:
        # Read original image
        data = read_func(key)
        if not data:
            logger.warning(f"Could not read: {key}")
            return False
        
        # Generate thumbnail
        thumb_data = generate_thumbnail(data, THUMB_SMALL, quality=98)
        if not thumb_data:
            logger.warning(f"Could not generate thumbnail for: {key}")
            return False
        
        thumb_key = get_thumbnail_key(key, 'small')
        
        if dry_run:
            logger.info(f"[DRY-RUN] Would upload thumbnail: {thumb_key} ({len(thumb_data)} bytes)")
            return True
        
        # Upload thumbnail
        bucket.put_object(
            Key=thumb_key,
            Body=thumb_data,
            ContentType='image/jpeg',
            ACL='private',
            CacheControl='public, max-age=31536000'  # 1 year cache
        )
        logger.info(f"Generated thumbnail: {thumb_key} ({len(thumb_data)} bytes)")
        return True
    except Exception as ex:
        logger.error(f"Error processing {key}: {ex}")
        return False


def scan_and_generate(bucket, prefix: str, read_func, dry_run: bool = False, limit: int = 0) -> tuple[int, int]:
    """Scan a prefix and generate missing thumbnails."""
    processed = 0
    generated = 0
    
    try:
        for obj in bucket.objects.filter(Prefix=prefix):
            key = obj.key
            
            # Skip non-images
            if not is_image_key(key):
                continue
            
            # Skip existing thumbnails
            if '_thumb_' in key:
                continue
            
            # Skip if thumbnail already exists
            if has_thumbnail(bucket, key):
                continue
            
            processed += 1
            
            if generate_and_upload_thumbnail(bucket, key, read_func, dry_run):
                generated += 1
            
            # Check limit
            if limit > 0 and generated >= limit:
                logger.info(f"Reached limit of {limit} thumbnails")
                break
    except Exception as ex:
        logger.error(f"Error scanning {prefix}: {ex}")
    
    return processed, generated


def get_all_user_uids(bucket) -> list[str]:
    """Get all user UIDs from the bucket."""
    uids = set()
    try:
        paginator = bucket.meta.client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket.name, Prefix='users/', Delimiter='/'):
            for prefix in page.get('CommonPrefixes', []):
                # Extract UID from users/{uid}/
                parts = prefix.get('Prefix', '').strip('/').split('/')
                if len(parts) >= 2:
                    uids.add(parts[1])
    except Exception as ex:
        logger.error(f"Error listing users: {ex}")
    return sorted(uids)


def main():
    parser = argparse.ArgumentParser(description='Generate missing thumbnails for existing images')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without making changes')
    parser.add_argument('--limit', type=int, default=0, help='Maximum number of thumbnails to generate (0 = unlimited)')
    parser.add_argument('--user', type=str, help='Process only this user UID')
    parser.add_argument('--backup', action='store_true', help='Process backup bucket (B2) instead of primary (R2)')
    args = parser.parse_args()
    
    # Select bucket
    if args.backup:
        if not s3_backup or not BACKUP_BUCKET:
            logger.error("Backup storage not configured")
            sys.exit(1)
        bucket = s3_backup.Bucket(BACKUP_BUCKET)
        read_func = backup_read_bytes_key
        bucket_name = BACKUP_BUCKET
    else:
        if not s3 or not R2_BUCKET:
            logger.error("Primary storage not configured")
            sys.exit(1)
        bucket = s3.Bucket(R2_BUCKET)
        read_func = read_bytes_key
        bucket_name = R2_BUCKET
    
    logger.info(f"{'[DRY-RUN] ' if args.dry_run else ''}Processing bucket: {bucket_name}")
    
    # Get user UIDs to process
    if args.user:
        uids = [args.user]
    else:
        uids = get_all_user_uids(bucket)
        logger.info(f"Found {len(uids)} users to process")
    
    total_processed = 0
    total_generated = 0
    remaining_limit = args.limit
    
    for uid in uids:
        logger.info(f"Processing user: {uid}")
        
        for prefix_template in SCAN_PREFIXES:
            prefix = prefix_template.format(uid=uid)
            
            current_limit = remaining_limit if remaining_limit > 0 else 0
            processed, generated = scan_and_generate(
                bucket, prefix, read_func, 
                dry_run=args.dry_run, 
                limit=current_limit
            )
            
            total_processed += processed
            total_generated += generated
            
            if args.limit > 0:
                remaining_limit -= generated
                if remaining_limit <= 0:
                    break
        
        if args.limit > 0 and remaining_limit <= 0:
            break
    
    logger.info(f"{'[DRY-RUN] ' if args.dry_run else ''}Complete: {total_generated}/{total_processed} thumbnails generated")


if __name__ == '__main__':
    main()
