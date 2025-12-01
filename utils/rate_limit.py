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
        import redis
        # Configure Redis with connection pooling and timeouts
        redis_client = redis.from_url(
            redis_url,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        # Test connection
        redis_client.ping()
        storage = store.RedisStore(redis_client)
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
