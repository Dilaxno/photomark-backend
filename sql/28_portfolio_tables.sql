-- Portfolio tables
-- Stores portfolio photos and settings for users
-- Run this migration on Neon PostgreSQL

-- Create the portfolio_settings table
CREATE TABLE IF NOT EXISTS portfolio_settings (
    uid VARCHAR(128) PRIMARY KEY,
    title VARCHAR(255) NOT NULL DEFAULT 'My Portfolio',
    subtitle VARCHAR(500),
    template VARCHAR(50) NOT NULL DEFAULT 'canvas',
    custom_domain VARCHAR(255),
    is_published BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ
);

-- Create the portfolio_photos table
CREATE TABLE IF NOT EXISTS portfolio_photos (
    id VARCHAR(128) PRIMARY KEY,
    uid VARCHAR(128) NOT NULL,
    url TEXT NOT NULL,
    title VARCHAR(255),
    "order" INTEGER NOT NULL DEFAULT 0,
    source VARCHAR(50) NOT NULL DEFAULT 'upload',
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Create the portfolio_domains table
CREATE TABLE IF NOT EXISTS portfolio_domains (
    id SERIAL PRIMARY KEY,
    uid VARCHAR(128) NOT NULL,
    hostname VARCHAR(255) NOT NULL UNIQUE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ssl_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ssl_verified BOOLEAN NOT NULL DEFAULT FALSE,
    ssl_verified_at TIMESTAMPTZ,
    dns_verified BOOLEAN NOT NULL DEFAULT FALSE,
    dns_verified_at TIMESTAMPTZ,
    dns_challenge_token VARCHAR(255),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_checked TIMESTAMPTZ
);

-- Create indexes for fast lookups
CREATE INDEX IF NOT EXISTS ix_portfolio_settings_uid ON portfolio_settings(uid);
CREATE INDEX IF NOT EXISTS ix_portfolio_photos_uid ON portfolio_photos(uid);
CREATE INDEX IF NOT EXISTS ix_portfolio_photos_uid_order ON portfolio_photos(uid, "order");
CREATE INDEX IF NOT EXISTS ix_portfolio_domains_uid ON portfolio_domains(uid);
CREATE INDEX IF NOT EXISTS ix_portfolio_domains_hostname ON portfolio_domains(hostname);
CREATE INDEX IF NOT EXISTS ix_portfolio_domains_hostname_verified ON portfolio_domains(hostname, dns_verified);

-- Add comments for documentation
COMMENT ON TABLE portfolio_settings IS 'Portfolio configuration and settings for users';
COMMENT ON TABLE portfolio_photos IS 'Photos in user portfolios';
COMMENT ON TABLE portfolio_domains IS 'Custom domain configuration for portfolio pages';

COMMENT ON COLUMN portfolio_settings.uid IS 'Firebase Auth user UID - owner of this portfolio';
COMMENT ON COLUMN portfolio_settings.template IS 'Portfolio template: canvas, editorial, noir';
COMMENT ON COLUMN portfolio_photos.uid IS 'Firebase Auth user UID - owner of this photo';
COMMENT ON COLUMN portfolio_photos.source IS 'Photo source: upload, gallery';
COMMENT ON COLUMN portfolio_domains.uid IS 'Firebase Auth user UID - owner of this domain';
COMMENT ON COLUMN portfolio_domains.hostname IS 'Custom domain hostname (e.g., portfolio.example.com)';

-- Create triggers to auto-update updated_at
CREATE OR REPLACE FUNCTION update_portfolio_settings_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION update_portfolio_photos_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION update_portfolio_domains_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Drop existing triggers if they exist
DROP TRIGGER IF EXISTS portfolio_settings_updated_at ON portfolio_settings;
DROP TRIGGER IF EXISTS portfolio_photos_updated_at ON portfolio_photos;
DROP TRIGGER IF EXISTS portfolio_domains_updated_at ON portfolio_domains;

-- Create triggers
CREATE TRIGGER portfolio_settings_updated_at
    BEFORE UPDATE ON portfolio_settings
    FOR EACH ROW
    EXECUTE FUNCTION update_portfolio_settings_updated_at();

CREATE TRIGGER portfolio_photos_updated_at
    BEFORE UPDATE ON portfolio_photos
    FOR EACH ROW
    EXECUTE FUNCTION update_portfolio_photos_updated_at();

CREATE TRIGGER portfolio_domains_updated_at
    BEFORE UPDATE ON portfolio_domains
    FOR EACH ROW
    EXECUTE FUNCTION update_portfolio_domains_updated_at();

-- Grant permissions (adjust role name as needed)
-- GRANT SELECT, INSERT, UPDATE, DELETE ON portfolio_settings TO your_app_role;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON portfolio_photos TO your_app_role;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON portfolio_domains TO your_app_role;