-- Fix storage columns to use BIGINT instead of INTEGER
-- INTEGER max is 2,147,483,647 (~2GB) but we need to store 5GB+ values

-- Alter storage_used_bytes to BIGINT
ALTER TABLE public.users 
ALTER COLUMN storage_used_bytes TYPE BIGINT;

-- Alter storage_limit_bytes to BIGINT  
ALTER TABLE public.users 
ALTER COLUMN storage_limit_bytes TYPE BIGINT;

-- Set default for storage_limit_bytes (5GB = 5368709120 bytes)
ALTER TABLE public.users 
ALTER COLUMN storage_limit_bytes SET DEFAULT 5368709120;
