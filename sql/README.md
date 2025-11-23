# SQL Scripts for Neon PostgreSQL

This directory contains SQL scripts for managing and querying your Neon PostgreSQL database.

## Connection

You can execute these queries using:

**1. Neon SQL Editor (Web)**
- Go to https://console.neon.tech
- Select your project → SQL Editor
- Copy and paste queries

**2. psql CLI**
```bash
psql 'postgresql://neondb_owner:npg_6vAguC5qRnQV@ep-rough-mouse-adtxfgjn-pooler.c-2.us-east-1.aws.neon.tech/neondb?sslmode=require'
```

**3. Python (from backend)**
```python
from core.database import SessionLocal
db = SessionLocal()
result = db.execute("SELECT * FROM users LIMIT 10")
for row in result:
    print(row)
```

## Available Scripts

### `schema_check.sql`
Check database schema, tables, columns, and indexes.
- List all tables
- View column definitions for each table
- Check indexes and constraints

**Use when:** Setting up database, verifying migration, debugging schema issues

### `users_queries.sql`
Common queries for user management:
- Get all users with plan info
- Count users by plan type
- Find paid vs free users
- Search users by email
- Get active/inactive users
- Storage usage analytics
- Affiliate tracking

**Use when:** Analyzing user base, debugging user issues, generating reports

### `shops_queries.sql`
Common queries for shop management:
- Get all shops with owner info
- Search shops by name/slug
- Count products per shop
- Get shops by theme settings
- Check slug mappings
- Find shops without products
- Flatten products across shops

**Use when:** Debugging shop setup, analyzing shop data, generating reports

### `maintenance.sql`
Database health and performance queries:
- Check database/table sizes
- Get row counts
- Monitor connections
- Check for locks
- Find slow queries
- Analyze index usage
- Vacuum and analyze tables

**Use when:** Performance tuning, troubleshooting, regular maintenance

## Quick Examples

### Get total users by plan
```sql
SELECT plan, COUNT(*) as count 
FROM users 
GROUP BY plan;
```

### Find shop by slug
```sql
SELECT s.*, u.email as owner_email
FROM shops s
LEFT JOIN users u ON s.owner_uid = u.uid
WHERE s.slug = 'my-shop-slug';
```

### Check database size
```sql
SELECT pg_size_pretty(pg_database_size(current_database()));
```

## Best Practices

1. **Always use LIMIT** when exploring data
2. **Use transactions** for multiple updates
   ```sql
   BEGIN;
   UPDATE users SET plan = 'pro' WHERE uid = 'xxx';
   -- Check results before commit
   COMMIT;  -- or ROLLBACK;
   ```
3. **Create indexes** for frequently queried columns
4. **Regular maintenance**: Run ANALYZE weekly for query optimization
5. **Monitor performance**: Check slow queries and index usage monthly

## Common Tasks

### Update user plan
```sql
UPDATE users 
SET plan = 'pro', updated_at = NOW()
WHERE email = 'user@example.com';
```

### Update shop theme
```sql
UPDATE shops
SET theme = theme || '{"fontFamily": "Poppins"}'::jsonb,
    updated_at = NOW()
WHERE slug = 'my-shop';
```

### Delete shop (with slug)
```sql
BEGIN;
DELETE FROM shop_slugs WHERE uid = 'shop_uid';
DELETE FROM shops WHERE uid = 'shop_uid';
COMMIT;
```

### Backup query results
```sql
COPY (SELECT * FROM users WHERE plan = 'pro') 
TO '/tmp/pro_users.csv' CSV HEADER;
```

## Troubleshooting

**Connection timeout?**
- Check if DATABASE_URL is set in .env
- Verify Neon project is not sleeping (free tier sleeps after inactivity)

**Permission denied?**
- Ensure you're using the correct user credentials
- Check Neon console for user permissions

**Slow queries?**
- Run `ANALYZE` on affected tables
- Check `maintenance.sql` for index usage
- Consider adding indexes on frequently filtered columns
