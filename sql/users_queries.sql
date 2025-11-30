-- Common queries for users table

-- 1. Get all users with their plan info
SELECT 
    uid,
    email,
    display_name,
    plan,
    is_active,
    email_verified,
    created_at,
    last_login_at
FROM users
ORDER BY created_at DESC
LIMIT 50;

-- 2. Count users by plan
SELECT 
    plan,
    COUNT(*) as user_count
FROM users
GROUP BY plan
ORDER BY user_count DESC;

-- 3. Get paid users only
SELECT 
    uid,
    email,
    display_name,
    plan,
    stripe_customer_id,
    subscription_status
FROM users
WHERE plan IN ('pro', 'business', 'enterprise', 'agencies')
ORDER BY created_at DESC;

-- 4. Find user by email
SELECT *
FROM users
WHERE email LIKE '%@example.com%';

-- 5. Get user by UID
SELECT *
FROM users
WHERE uid = 'YOUR_UID_HERE';

-- 6. Get recently active users (last 7 days)
SELECT 
    uid,
    email,
    display_name,
    plan,
    last_login_at
FROM users
WHERE last_login_at > NOW() - INTERVAL '7 days'
ORDER BY last_login_at DESC;

-- 7. Get inactive users (no login in 30 days)
SELECT 
    uid,
    email,
    display_name,
    created_at,
    last_login_at
FROM users
WHERE last_login_at < NOW() - INTERVAL '30 days'
   OR last_login_at IS NULL
ORDER BY created_at DESC;

-- 8. Get user storage usage stats
SELECT 
    plan,
    COUNT(*) as users,
    AVG(storage_used_bytes) / 1048576 as avg_mb_used,
    SUM(storage_used_bytes) / 1073741824 as total_gb_used
FROM users
GROUP BY plan
ORDER BY total_gb_used DESC;

-- 9. Get users with affiliate earnings
SELECT 
    uid,
    email,
    display_name,
    affiliate_code,
    affiliate_earnings,
    referred_by
FROM users
WHERE affiliate_earnings > 0
ORDER BY affiliate_earnings DESC;

-- 10. Update user plan (example)
-- UPDATE users 
-- SET plan = 'pro', updated_at = NOW()
-- WHERE uid = 'YOUR_UID_HERE';

-- 11. Get users close to storage limit (90%+)
SELECT 
    uid,
    email,
    plan,
    storage_used_bytes / 1048576 as used_mb,
    storage_limit_bytes / 1048576 as limit_mb,
    ROUND((storage_used_bytes::numeric / storage_limit_bytes * 100), 2) as percent_used
FROM users
WHERE storage_used_bytes::numeric / storage_limit_bytes > 0.9
ORDER BY percent_used DESC;

-- 12. Get admin users
SELECT *
FROM users
WHERE is_admin = true;

-- 13. Count new signups by month
SELECT 
    DATE_TRUNC('month', created_at) as signup_month,
    COUNT(*) as new_users
FROM users
GROUP BY signup_month
ORDER BY signup_month DESC;
