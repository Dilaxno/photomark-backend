-- Create portfolio_slugs table for user-friendly portfolio URLs
-- Similar to shop_slugs but for portfolios

CREATE TABLE IF NOT EXISTS portfolio_slugs (
    slug VARCHAR(100) PRIMARY KEY,
    uid VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_portfolio_slugs_uid ON portfolio_slugs(uid);
CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolio_slug_uid_unique ON portfolio_slugs(uid);

-- Add trigger to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_portfolio_slugs_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_portfolio_slugs_updated_at
    BEFORE UPDATE ON portfolio_slugs
    FOR EACH ROW
    EXECUTE FUNCTION update_portfolio_slugs_updated_at();

-- Add comment
COMMENT ON TABLE portfolio_slugs IS 'Maps user-friendly slugs to UIDs for portfolio URLs';