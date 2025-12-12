-- Add blocked column to collaborators table
-- This allows owners to block collaborators from signing in without deleting them

ALTER TABLE collaborators ADD COLUMN IF NOT EXISTS blocked BOOLEAN DEFAULT FALSE;

-- Create index for faster lookups
CREATE INDEX IF NOT EXISTS idx_collaborators_blocked ON collaborators(blocked);
