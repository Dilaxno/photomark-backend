-- Uploads Custom Domains table
-- Stores custom domain configuration for user uploads preview pages
-- Run this migration on Neon PostgreSQL

-- Create the uploads_domains table
CREATE TABLE IF NOT EXISTS uploads_domains (
    id VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid()::text,
    uid VARCHAR(128) NOT NULL UNIQUE,
    hostname VARCHAR(255) NOT NULL UNIQUE,
    dns_verified BOOLEAN NOT NULL DEFAULT FALSE,
    ssl_status VARCHAR(32) NOT NULL DEFAULT 'unknown',
    cname_observed VARCHAR(255),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    last_error VARCHAR(512),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_checked TIMESTAMPTZ
);

-- Create indexes for fast lookups
CREATE INDEX IF NOT EXISTS ix_uploads_domains_uid ON uploads_domains(uid);
CREATE INDEX IF NOT EXISTS ix_uploads_domains_hostname ON uploads_domains(hostname);
CREATE INDEX IF NOT EXISTS ix_uploads_domains_hostname_verified ON uploads_domains(hostname, dns_verified);

-- Add comment for documentation
COMMENT ON TABLE uploads_domains IS 'Custom domain configuration for user uploads preview pages';
COMMENT ON COLUMN uploads_domains.uid IS 'Firebase Auth user UID - owner of this domain';
COMMENT ON COLUMN uploads_domains.hostname IS 'Custom domain hostname (e.g., photos.example.com)';
COMMENT ON COLUMN uploads_domains.dns_verified IS 'Whether DNS CNAME is verified pointing to api.photomark.cloud';
COMMENT ON COLUMN uploads_domains.ssl_status IS 'SSL certificate status: unknown, pending, active, blocked';
COMMENT ON COLUMN uploads_domains.enabled IS 'Whether the custom domain is enabled for use';

-- Create trigger to auto-update updated_at
CREATE OR REPLACE FUNCTION update_uploads_domains_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS uploads_domains_updated_at ON uploads_domains;
CREATE TRIGGER uploads_domains_updated_at
    BEFORE UPDATE ON uploads_domains
    FOR EACH ROW
    EXECUTE FUNCTION update_uploads_domains_updated_at();

-- Grant permissions (adjust role name as needed)
-- GRANT SELECT, INSERT, UPDATE, DELETE ON uploads_domains TO your_app_role;
