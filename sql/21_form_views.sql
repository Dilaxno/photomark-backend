-- Form Views table for tracking unique views
-- This prevents double counting views from the same visitor

CREATE TABLE IF NOT EXISTS form_views (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    form_id UUID NOT NULL REFERENCES booking_forms(id) ON DELETE CASCADE,
    visitor_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for efficient lookups
CREATE INDEX IF NOT EXISTS ix_form_views_form_id ON form_views(form_id);
CREATE INDEX IF NOT EXISTS ix_form_views_visitor_hash ON form_views(visitor_hash);
CREATE INDEX IF NOT EXISTS ix_form_views_created_at ON form_views(created_at);
CREATE INDEX IF NOT EXISTS ix_form_views_form_visitor ON form_views(form_id, visitor_hash);

-- Clean up old view records (older than 30 days) to save space
-- Run this periodically via a cron job or scheduled task
-- DELETE FROM form_views WHERE created_at < NOW() - INTERVAL '30 days';
