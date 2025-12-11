-- User Security Tables for Neon PostgreSQL
-- Run this migration to add security-related tables

-- User Security Settings Table
CREATE TABLE IF NOT EXISTS user_security (
    uid VARCHAR(128) PRIMARY KEY REFERENCES users(uid) ON DELETE CASCADE,
    secondary_email VARCHAR(255),
    secondary_email_verified BOOLEAN DEFAULT FALSE,
    phone_number VARCHAR(20),
    phone_verified BOOLEAN DEFAULT FALSE,
    phone_country_code VARCHAR(5),
    two_factor_enabled BOOLEAN DEFAULT FALSE,
    two_factor_method VARCHAR(20),
    totp_secret VARCHAR(64),
    backup_codes JSONB DEFAULT '[]'::jsonb,
    backup_codes_generated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

-- Create index on secondary_email for lookups
CREATE INDEX IF NOT EXISTS idx_user_security_secondary_email ON user_security(secondary_email);

-- Password Reset Requests Table
CREATE TABLE IF NOT EXISTS password_reset_requests (
    id SERIAL PRIMARY KEY,
    uid VARCHAR(128) NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
    email VARCHAR(255) NOT NULL,
    code VARCHAR(64) NOT NULL,
    verified BOOLEAN DEFAULT FALSE,
    method VARCHAR(20) DEFAULT 'email',
    expires_at TIMESTAMPTZ NOT NULL,
    used BOOLEAN DEFAULT FALSE,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

-- Create indexes for password reset lookups
CREATE INDEX IF NOT EXISTS idx_password_reset_uid ON password_reset_requests(uid);
CREATE INDEX IF NOT EXISTS idx_password_reset_email ON password_reset_requests(email);
CREATE INDEX IF NOT EXISTS idx_password_reset_code ON password_reset_requests(code);

-- SMS Verification Codes Table
CREATE TABLE IF NOT EXISTS sms_verification_codes (
    id SERIAL PRIMARY KEY,
    uid VARCHAR(128) NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
    code VARCHAR(10) NOT NULL,
    purpose VARCHAR(30) DEFAULT 'password_reset',
    phone_number VARCHAR(20) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    used BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

-- Create index for SMS code lookups
CREATE INDEX IF NOT EXISTS idx_sms_codes_uid ON sms_verification_codes(uid);

-- Add trigger to auto-update updated_at on user_security
CREATE OR REPLACE FUNCTION update_user_security_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_user_security_updated_at ON user_security;
CREATE TRIGGER trigger_user_security_updated_at
    BEFORE UPDATE ON user_security
    FOR EACH ROW
    EXECUTE FUNCTION update_user_security_updated_at();

-- Clean up expired records (run periodically via cron or scheduled task)
-- DELETE FROM password_reset_requests WHERE expires_at < NOW() - INTERVAL '1 day';
-- DELETE FROM sms_verification_codes WHERE expires_at < NOW() - INTERVAL '1 day';
