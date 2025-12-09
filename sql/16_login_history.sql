-- Login history table for tracking user login sessions
-- Used for security notifications and displaying recent logins in settings

CREATE TABLE IF NOT EXISTS public.login_history (
    id SERIAL PRIMARY KEY,
    uid TEXT NOT NULL REFERENCES public.users(uid) ON DELETE CASCADE,
    ip_address VARCHAR(45) NOT NULL,
    city VARCHAR(100),
    country VARCHAR(100),
    country_code VARCHAR(2),  -- ISO 3166-1 alpha-2 for country flags
    logged_in_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for fast lookups by user
CREATE INDEX IF NOT EXISTS ix_login_history_uid ON public.login_history (uid);

-- Index for ordering by time
CREATE INDEX IF NOT EXISTS ix_login_history_logged_in_at ON public.login_history (logged_in_at DESC);

-- Composite index for user + time queries
CREATE INDEX IF NOT EXISTS ix_login_history_uid_time ON public.login_history (uid, logged_in_at DESC);

COMMENT ON TABLE public.login_history IS 'Tracks user login sessions for security notifications and audit';
COMMENT ON COLUMN public.login_history.country_code IS 'ISO 3166-1 alpha-2 country code for displaying flags';
