-- Database maintenance queries for Neon PostgreSQL

-- 1. Get database size
SELECT 
    pg_database.datname,
    pg_size_pretty(pg_database_size(pg_database.datname)) AS size
FROM pg_database
ORDER BY pg_database_size(pg_database.datname) DESC;

-- 2. Get table sizes
SELECT 
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size,
    pg_size_pretty(pg_relation_size(schemaname||'.'||tablename)) AS table_size,
    pg_size_pretty(pg_indexes_size(schemaname||'.'||tablename)) AS indexes_size
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;

-- 3. Get row counts for all tables
SELECT 
    schemaname,
    relname as table_name,
    n_live_tup as row_count
FROM pg_stat_user_tables
WHERE schemaname = 'public'
ORDER BY n_live_tup DESC;

-- 4. Check table bloat and last vacuum
SELECT 
    schemaname,
    relname,
    n_live_tup,
    n_dead_tup,
    last_vacuum,
    last_autovacuum,
    last_analyze
FROM pg_stat_user_tables
WHERE schemaname = 'public'
ORDER BY n_dead_tup DESC;

-- 5. View active connections
SELECT 
    pid,
    usename,
    application_name,
    client_addr,
    state,
    query_start,
    state_change,
    LEFT(query, 100) as query_preview
FROM pg_stat_activity
WHERE datname = current_database()
ORDER BY query_start DESC;

-- 6. Kill long-running queries (if needed)
-- SELECT pg_terminate_backend(pid)
-- FROM pg_stat_activity
-- WHERE state = 'active' 
--   AND query_start < NOW() - INTERVAL '5 minutes'
--   AND pid != pg_backend_pid();

-- 7. Check for locks
SELECT 
    l.pid,
    l.mode,
    l.granted,
    d.datname,
    c.relname,
    a.usename,
    a.query_start,
    a.state
FROM pg_locks l
JOIN pg_database d ON l.database = d.oid
LEFT JOIN pg_class c ON l.relation = c.oid
LEFT JOIN pg_stat_activity a ON l.pid = a.pid
WHERE d.datname = current_database()
ORDER BY l.pid;

-- 8. Analyze all tables (updates statistics)
-- ANALYZE users;
-- ANALYZE shops;
-- ANALYZE shop_slugs;
-- ANALYZE collaborator_access;

-- 9. Vacuum tables (reclaim space)
-- VACUUM ANALYZE users;
-- VACUUM ANALYZE shops;

-- 10. Get most frequently accessed tables
SELECT 
    schemaname,
    relname,
    seq_scan,
    seq_tup_read,
    idx_scan,
    idx_tup_fetch,
    n_tup_ins,
    n_tup_upd,
    n_tup_del
FROM pg_stat_user_tables
WHERE schemaname = 'public'
ORDER BY seq_scan + idx_scan DESC;

-- 11. Check index usage
SELECT
    schemaname,
    tablename,
    indexname,
    idx_scan,
    idx_tup_read,
    idx_tup_fetch
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
ORDER BY idx_scan DESC;

-- 12. Find unused indexes (candidates for removal)
SELECT
    schemaname,
    tablename,
    indexname,
    idx_scan,
    pg_size_pretty(pg_relation_size(indexrelid)) as index_size
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
  AND idx_scan = 0
  AND indexrelname NOT LIKE '%_pkey'  -- Exclude primary keys
ORDER BY pg_relation_size(indexrelid) DESC;
