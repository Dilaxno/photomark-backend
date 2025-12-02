-- Migration: Add published column to shops table
-- This column controls whether a shop is publicly visible or only accessible by the owner

-- Add published column with default value of false
ALTER TABLE shops ADD COLUMN IF NOT EXISTS published BOOLEAN NOT NULL DEFAULT FALSE;

-- Create index for faster queries on published shops
CREATE INDEX IF NOT EXISTS idx_shops_published ON shops(published);

-- Comment for documentation
COMMENT ON COLUMN shops.published IS 'If false, only the shop owner can view the shop. If true, the shop is publicly accessible.';
