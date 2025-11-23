-- Common queries for shops and shop_slugs tables

-- 1. Get all shops with owner info
SELECT 
    s.uid,
    s.name,
    s.slug,
    s.owner_uid,
    s.owner_name,
    s.description,
    s.created_at,
    s.updated_at,
    u.email as owner_email,
    u.plan as owner_plan
FROM shops s
LEFT JOIN users u ON s.owner_uid = u.uid
ORDER BY s.created_at DESC;

-- 2. Get shop by slug
SELECT *
FROM shops
WHERE slug = 'your-shop-slug';

-- 3. Get shop by UID
SELECT *
FROM shops
WHERE uid = 'YOUR_UID_HERE';

-- 4. Count products per shop
SELECT 
    uid,
    name,
    slug,
    jsonb_array_length(products::jsonb) as product_count,
    created_at
FROM shops
WHERE products IS NOT NULL
ORDER BY product_count DESC;

-- 5. Get shops with specific theme color
SELECT 
    uid,
    name,
    slug,
    theme->>'primaryColor' as primary_color,
    theme->>'fontFamily' as font
FROM shops
WHERE theme->>'primaryColor' IS NOT NULL
ORDER BY created_at DESC;

-- 6. Find shops by name (search)
SELECT 
    uid,
    name,
    slug,
    description,
    owner_name
FROM shops
WHERE name ILIKE '%search-term%'
   OR description ILIKE '%search-term%'
ORDER BY created_at DESC;

-- 7. Get shop_slugs mapping
SELECT 
    ss.slug,
    ss.uid,
    s.name,
    s.owner_uid,
    ss.updated_at
FROM shop_slugs ss
LEFT JOIN shops s ON ss.uid = s.uid
ORDER BY ss.updated_at DESC;

-- 8. Check for slug conflicts
SELECT 
    slug,
    COUNT(*) as count
FROM shop_slugs
GROUP BY slug
HAVING COUNT(*) > 1;

-- 9. Get shops without products
SELECT 
    uid,
    name,
    slug,
    created_at
FROM shops
WHERE products IS NULL 
   OR jsonb_array_length(products::jsonb) = 0
ORDER BY created_at DESC;

-- 10. Get shops created in last 7 days
SELECT 
    uid,
    name,
    slug,
    owner_name,
    created_at
FROM shops
WHERE created_at > NOW() - INTERVAL '7 days'
ORDER BY created_at DESC;

-- 11. Update shop theme (example)
-- UPDATE shops
-- SET 
--     theme = theme || '{"fontFamily": "Poppins"}'::jsonb,
--     updated_at = NOW()
-- WHERE uid = 'YOUR_UID_HERE';

-- 12. Get shops with custom logo/banner
SELECT 
    uid,
    name,
    slug,
    theme->>'logoUrl' as logo_url,
    theme->>'bannerUrl' as banner_url
FROM shops
WHERE theme->>'logoUrl' IS NOT NULL 
   OR theme->>'bannerUrl' IS NOT NULL;

-- 13. Delete shop and its slug mapping (example)
-- BEGIN;
-- DELETE FROM shop_slugs WHERE uid = 'YOUR_UID_HERE';
-- DELETE FROM shops WHERE uid = 'YOUR_UID_HERE';
-- COMMIT;

-- 14. Get all products across all shops (flatten)
SELECT 
    s.uid as shop_uid,
    s.name as shop_name,
    s.slug as shop_slug,
    p->>'id' as product_id,
    p->>'title' as product_title,
    (p->>'price')::numeric as product_price,
    p->>'category' as product_category
FROM shops s,
     jsonb_array_elements(s.products::jsonb) as p
WHERE s.products IS NOT NULL
ORDER BY s.created_at DESC;

-- 15. Count shops per user (find users with multiple shops)
SELECT 
    owner_uid,
    owner_name,
    COUNT(*) as shop_count,
    array_agg(name) as shop_names
FROM shops
GROUP BY owner_uid, owner_name
HAVING COUNT(*) > 1
ORDER BY shop_count DESC;
