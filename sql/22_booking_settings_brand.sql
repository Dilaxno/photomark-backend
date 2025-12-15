-- Add branding columns to booking_settings table
-- These columns are used for form styling and branding

ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS business_logo TEXT;
ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS brand_logo TEXT;
ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS brand_primary_color VARCHAR(7) DEFAULT '#6366f1';
ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS brand_secondary_color VARCHAR(7) DEFAULT '#8b5cf6';
ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS brand_text_color VARCHAR(7) DEFAULT '#1f2937';
ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS brand_background_color VARCHAR(7) DEFAULT '#ffffff';
