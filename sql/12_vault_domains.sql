-- Vault Custom Domains table
-- Stores custom domain configuration for vault share pages

CREATE TABLE IF NOT EXISTS vault_domains (
    id VARCHAR(36) PRIMARY KEY,
    uid VARCHAR(128) NOT NULL,
    vault_name VARCHAR(255) NOT NULL,
    share_token VARCHAR(128),
    hostname VARCHAR(255) NOT NULL UNIQUE,
    dns_verified BOOLEAN NOT NULL DEFAULT FALSE,
    ssl_status VARCHAR(32) NOT NULL DEFAULT 'unknown',
    cname_observed VARCHAR(255),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    last_error VARCHAR(512),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    last_checked TIMESTAMP WITH TIME ZONE
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS ix_vault_domains_uid ON vault_domains(uid);
CREATE INDEX IF NOT EXISTS ix_vault_domains_hostname ON vault_domains(hostname);
CREATE INDEX IF NOT EXISTS ix_vault_domains_hostname_verified ON vault_domains(hostname, dns_verified);
CREATE INDEX IF NOT EXISTS ix_vault_domains_uid_vault ON vault_domains(uid, vault_name);

-- Comment
COMMENT ON TABLE vault_domains IS 'Custom domain configuration for vault share pages';
