-- Add secondary_email column to users table for backup/recovery email
-- This allows users to have a secondary email for account recovery and notifications

ALTER TABLE public.users 
ADD COLUMN IF NOT EXISTS secondary_email VARCHAR(255);

-- Index for lookups
CREATE INDEX IF NOT EXISTS ix_users_secondary_email ON public.users (secondary_email);

COMMENT ON COLUMN public.users.secondary_email IS 'Secondary/backup email for account recovery and notifications';
