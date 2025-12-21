-- Login history table for tracking user login sessions
-- Used for security notifications and displaying recent logins in settings

-- Migration: Add new columns if table already exists (run this first)
DO $$
BEGIN
    -- Check if table exists first
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'login_history') THEN
        -- Add new columns if they don't exist
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'login_history' AND column_name = 'user_agent') THEN
            ALTER TABLE public.login_history ADD COLUMN user_agent VARCHAR(512);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'login_history' AND column_name = 'source') THEN
            ALTER TABLE public.login_history ADD COLUMN source VARCHAR(50) DEFAULT 'web';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'login_history' AND column_name = 'success') THEN
            ALTER TABLE public.login_history ADD COLUMN success BOOLEAN DEFAULT TRUE;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'login_history' AND column_name = 'failure_reason') THEN
            ALTER TABLE public.login_history ADD COLUMN failure_reason VARCHAR(255);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'login_history' AND column_name = 'attempted_email') THEN
            ALTER TABLE public.login_history ADD COLUMN attempted_email VARCHAR(255);
        END IF;
        -- Make uid nullable for failed logins (if it's currently NOT NULL)
        ALTER TABLE public.login_history ALTER COLUMN uid DROP NOT NULL;
    END IF;
END $$;

-- Create table if it doesn't exist (for fresh installs)
CREATE TABLE IF NOT EXISTS public.login_history (
    id SERIAL PRIMARY KEY,
    uid TEXT REFERENCES public.users(uid) ON DELETE CASCADE,  -- nullable for failed logins
    ip_address VARCHAR(45) NOT NULL,
    city VARCHAR(100),
    country VARCHAR(100),
    country_code VARCHAR(2),  -- ISO 3166-1 alpha-2 for country flags
    user_agent VARCHAR(512),  -- Browser/client user agent
    source VARCHAR(50) DEFAULT 'web',  -- web, lightroom, photoshop, api
    success BOOLEAN DEFAULT TRUE,
    failure_reason VARCHAR(255),  -- For failed logins
    attempted_email VARCHAR(255),  -- Email for failed logins when uid is not available
    logged_in_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for fast lookups by user
CREATE INDEX IF NOT EXISTS ix_login_history_uid ON public.login_history (uid);

-- Index for ordering by time
CREATE INDEX IF NOT EXISTS ix_login_history_logged_in_at ON public.login_history (logged_in_at DESC);

-- Composite index for user + time queries
CREATE INDEX IF NOT EXISTS ix_login_history_uid_time ON public.login_history (uid, logged_in_at DESC);

-- Index for source filtering (only create if column exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'login_history' AND column_name = 'source') THEN
        CREATE INDEX IF NOT EXISTS ix_login_history_source ON public.login_history (source);
    END IF;
END $$;

-- Index for failed login monitoring (only create if column exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'login_history' AND column_name = 'success') THEN
        CREATE INDEX IF NOT EXISTS ix_login_history_success ON public.login_history (success) WHERE success = FALSE;
    END IF;
END $$;

COMMENT ON TABLE public.login_history IS 'Tracks user login sessions for security notifications and audit';
COMMENT ON COLUMN public.login_history.country_code IS 'ISO 3166-1 alpha-2 country code for displaying flags';
