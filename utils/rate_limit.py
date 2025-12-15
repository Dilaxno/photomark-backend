"""Rate limiting utilities using throttled-py"""
import os
import logging
from datetime import timedelta
from throttled import Throttled, RateLimiterType, store, rate_limiter

logger = logging.getLogger("photomark")

# Initialize storage - Redis for production, MemoryStore for development
_storage_type = "memory"
try:
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        # RedisStore expects the URL string, not a Redis client object
        storage = store.RedisStore(server=redis_url)
        _storage_type = "redis"
        logger.info("[rate_limit] Using Redis for rate limiting")
    else:
        storage = store.MemoryStore()
        logger.warning("[rate_limit] REDIS_URL not set - using in-memory storage (not suitable for production with multiple workers)")
except Exception as ex:
    # Fallback to memory storage if Redis is not available
    storage = store.MemoryStore()
    logger.warning(f"[rate_limit] Redis connection failed, using in-memory storage: {ex}")

# Signup limiter: 1 signup per IP per 6 hours
# Using Fixed Window algorithm with quota of 1 request per 6 hours
signup_throttle = Throttled(
    using=RateLimiterType.FIXED_WINDOW.value,
    quota=rate_limiter.per_duration(timedelta(hours=6), limit=1),
    store=storage,
)

# Password reset limiter: 5 requests per email per hour (prevent enumeration/abuse)
password_reset_throttle = Throttled(
    using=RateLimiterType.FIXED_WINDOW.value,
    quota=rate_limiter.per_duration(timedelta(hours=1), limit=5),
    store=storage,
)

# Login attempt limiter: 10 attempts per IP per 15 minutes (brute force protection)
login_throttle = Throttled(
    using=RateLimiterType.FIXED_WINDOW.value,
    quota=rate_limiter.per_duration(timedelta(minutes=15), limit=10),
    store=storage,
)

# Admin endpoint limiter: 30 requests per IP per minute
admin_throttle = Throttled(
    using=RateLimiterType.FIXED_WINDOW.value,
    quota=rate_limiter.per_duration(timedelta(minutes=1), limit=30),
    store=storage,
)

# Upload rate limiter: 100 uploads per user per hour (prevent storage abuse)
upload_throttle = Throttled(
    using=RateLimiterType.FIXED_WINDOW.value,
    quota=rate_limiter.per_duration(timedelta(hours=1), limit=100),
    store=storage,
)

# Upload size limiter: 500MB total per user per hour (prevent bandwidth abuse)
upload_size_throttle = Throttled(
    using=RateLimiterType.FIXED_WINDOW.value,
    quota=rate_limiter.per_duration(timedelta(hours=1), limit=500 * 1024 * 1024),  # 500MB in bytes
    store=storage,
)

# Anonymous/unauthenticated upload limiter: 10 uploads per IP per hour
anonymous_upload_throttle = Throttled(
    using=RateLimiterType.FIXED_WINDOW.value,
    quota=rate_limiter.per_duration(timedelta(hours=1), limit=10),
    store=storage,
)

# Processing endpoint limiter (upscaler, style transfer, etc.): 20 per user per hour
processing_throttle = Throttled(
    using=RateLimiterType.FIXED_WINDOW.value,
    quota=rate_limiter.per_duration(timedelta(hours=1), limit=20),
    store=storage,
)


# Maximum file size limits - Images
MAX_IMAGE_FILE_SIZE_MB = 50  # 50MB per image file
MAX_IMAGE_FILE_SIZE_BYTES = MAX_IMAGE_FILE_SIZE_MB * 1024 * 1024
MAX_IMAGE_BATCH_SIZE_MB = 1024  # 1GB per image batch upload
MAX_IMAGE_BATCH_SIZE_BYTES = MAX_IMAGE_BATCH_SIZE_MB * 1024 * 1024

# Maximum file size limits - Videos
MAX_VIDEO_FILE_SIZE_MB = 1024  # 1GB per video file
MAX_VIDEO_FILE_SIZE_BYTES = MAX_VIDEO_FILE_SIZE_MB * 1024 * 1024
MAX_VIDEO_BATCH_SIZE_MB = 5 * 1024  # 5GB per video batch upload
MAX_VIDEO_BATCH_SIZE_BYTES = MAX_VIDEO_BATCH_SIZE_MB * 1024 * 1024

# Legacy aliases for backward compatibility
MAX_FILE_SIZE_MB = MAX_IMAGE_FILE_SIZE_MB
MAX_FILE_SIZE_BYTES = MAX_IMAGE_FILE_SIZE_BYTES
MAX_BATCH_SIZE_MB = MAX_IMAGE_BATCH_SIZE_MB
MAX_BATCH_SIZE_BYTES = MAX_IMAGE_BATCH_SIZE_BYTES

MAX_FILES_PER_BATCH = 50  # Maximum files per single upload request

# File extensions for type detection
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif', '.tif', '.tiff', '.gif', '.bmp', '.raw', '.cr2', '.cr3', '.nef', '.arw', '.dng', '.orf', '.rw2', '.pef', '.srw'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.wmv', '.flv', '.3gp', '.mpeg', '.mpg', '.mts', '.m2ts'}


def is_video_file(filename: str) -> bool:
    """Check if a file is a video based on extension."""
    if not filename:
        return False
    ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''
    return f'.{ext}' in VIDEO_EXTENSIONS


def is_image_file(filename: str) -> bool:
    """Check if a file is an image based on extension."""
    if not filename:
        return True  # Default to image if unknown
    ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''
    return f'.{ext}' in IMAGE_EXTENSIONS or f'.{ext}' not in VIDEO_EXTENSIONS


def check_upload_rate_limit(user_id: str, file_count: int = 1, total_size_bytes: int = 0) -> tuple[bool, str]:
    """
    Check if upload is allowed based on rate limits.
    
    Args:
        user_id: User ID or IP address for anonymous users
        file_count: Number of files being uploaded
        total_size_bytes: Total size of all files in bytes
    
    Returns:
        Tuple of (allowed: bool, error_message: str)
    """
    try:
        # Check file count limit
        result = upload_throttle.limit(f"upload_count:{user_id}", cost=file_count)
        if result.limited:
            return False, f"Upload rate limit exceeded. You can upload up to 100 files per hour. Please try again later."
        
        # Check total size limit
        if total_size_bytes > 0:
            size_result = upload_size_throttle.limit(f"upload_size:{user_id}", cost=total_size_bytes)
            if size_result.limited:
                return False, f"Upload size limit exceeded. You can upload up to 500MB per hour. Please try again later."
        
        return True, ""
    except Exception as ex:
        logger.warning(f"[rate_limit] Upload rate limit check failed: {ex}")
        # Fail open - allow upload if rate limiter fails
        return True, ""


def check_anonymous_upload_rate_limit(ip: str) -> tuple[bool, str]:
    """
    Check if anonymous upload is allowed based on IP rate limits.
    
    Args:
        ip: Client IP address
    
    Returns:
        Tuple of (allowed: bool, error_message: str)
    """
    try:
        result = anonymous_upload_throttle.limit(f"anon_upload:{ip}", cost=1)
        if result.limited:
            return False, "Upload rate limit exceeded. Anonymous users can upload up to 10 files per hour. Please sign in for higher limits."
        return True, ""
    except Exception as ex:
        logger.warning(f"[rate_limit] Anonymous upload rate limit check failed: {ex}")
        return True, ""


def check_processing_rate_limit(user_id: str) -> tuple[bool, str]:
    """
    Check if processing request is allowed based on rate limits.
    
    Args:
        user_id: User ID or IP address
    
    Returns:
        Tuple of (allowed: bool, error_message: str)
    """
    try:
        result = processing_throttle.limit(f"processing:{user_id}", cost=1)
        if result.limited:
            return False, "Processing rate limit exceeded. You can process up to 20 images per hour. Please try again later."
        return True, ""
    except Exception as ex:
        logger.warning(f"[rate_limit] Processing rate limit check failed: {ex}")
        return True, ""


def validate_upload_request(files_count: int, total_size_bytes: int, has_videos: bool = False) -> tuple[bool, str]:
    """
    Validate upload request against size and count limits.
    
    Args:
        files_count: Number of files in the request
        total_size_bytes: Total size of all files
        has_videos: Whether the batch contains video files (uses higher limits)
    
    Returns:
        Tuple of (valid: bool, error_message: str)
    """
    if files_count > MAX_FILES_PER_BATCH:
        return False, f"Too many files. Maximum {MAX_FILES_PER_BATCH} files per upload."
    
    if has_videos:
        if total_size_bytes > MAX_VIDEO_BATCH_SIZE_BYTES:
            return False, f"Total upload size too large. Maximum {MAX_VIDEO_BATCH_SIZE_MB // 1024}GB for video uploads."
    else:
        if total_size_bytes > MAX_IMAGE_BATCH_SIZE_BYTES:
            return False, f"Total upload size too large. Maximum {MAX_IMAGE_BATCH_SIZE_MB // 1024}GB for image uploads."
    
    return True, ""


def validate_file_size(size_bytes: int, filename: str = "") -> tuple[bool, str]:
    """
    Validate individual file size based on file type (image vs video).
    
    Args:
        size_bytes: File size in bytes
        filename: Filename to determine type and for error message
    
    Returns:
        Tuple of (valid: bool, error_message: str)
    """
    name_part = f" '{filename}'" if filename else ""
    
    if is_video_file(filename):
        if size_bytes > MAX_VIDEO_FILE_SIZE_BYTES:
            return False, f"Video file{name_part} too large. Maximum video file size is {MAX_VIDEO_FILE_SIZE_MB // 1024}GB."
    else:
        if size_bytes > MAX_IMAGE_FILE_SIZE_BYTES:
            return False, f"Image file{name_part} too large. Maximum image file size is {MAX_IMAGE_FILE_SIZE_MB}MB."
    
    return True, ""


def validate_batch_with_files(filenames: list[str], sizes: list[int]) -> tuple[bool, str]:
    """
    Validate a batch of files with their sizes.
    
    Args:
        filenames: List of filenames
        sizes: List of file sizes in bytes (same order as filenames)
    
    Returns:
        Tuple of (valid: bool, error_message: str)
    """
    if len(filenames) > MAX_FILES_PER_BATCH:
        return False, f"Too many files. Maximum {MAX_FILES_PER_BATCH} files per upload."
    
    has_videos = any(is_video_file(f) for f in filenames)
    total_size = sum(sizes)
    
    # Validate total batch size
    if has_videos:
        if total_size > MAX_VIDEO_BATCH_SIZE_BYTES:
            return False, f"Total upload size too large. Maximum {MAX_VIDEO_BATCH_SIZE_MB // 1024}GB for batches with videos."
    else:
        if total_size > MAX_IMAGE_BATCH_SIZE_BYTES:
            return False, f"Total upload size too large. Maximum {MAX_IMAGE_BATCH_SIZE_MB // 1024}GB for image batches."
    
    # Validate individual file sizes
    for filename, size in zip(filenames, sizes):
        valid, err = validate_file_size(size, filename)
        if not valid:
            return False, err
    
    return True, ""
