"""Rate limiting utilities using throttled-py"""
import os
from datetime import timedelta
from throttled import Throttled, RateLimiterType, store, rate_limiter

# Initialize storage (MemoryStore for simple deployments, can upgrade to Redis later)
try:
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        import redis
        redis_client = redis.from_url(redis_url)
        storage = store.RedisStore(redis_client)
    else:
        storage = store.MemoryStore()
except Exception:
    # Fallback to memory storage if Redis is not available
    storage = store.MemoryStore()

# Signup limiter: 1 signup per IP per 6 hours
# Using Fixed Window algorithm with quota of 1 request per 6 hours
signup_throttle = Throttled(
    using=RateLimiterType.FIXED_WINDOW.value,
    quota=rate_limiter.per_duration(timedelta(hours=6), limit=1),
    store=storage,
)
