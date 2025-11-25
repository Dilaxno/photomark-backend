-- Create vaults table in Neon PostgreSQL
-- This stores vault metadata (logo, welcome message, settings)
-- Photo files remain in R2 storage

CREATE TABLE IF NOT EXISTS vaults (
    id SERIAL PRIMARY KEY,
    uid VARCHAR(128) NOT NULL,
    vault_name VARCHAR(255) NOT NULL,
    
    -- Display and branding
    display_name VARCHAR(255),
    logo_url TEXT,
    welcome_message TEXT,
    
    -- Protection settings
    protected BOOLEAN DEFAULT FALSE,
    password_hash VARCHAR(255),
    
    -- Share customization
    share_hide_ui BOOLEAN DEFAULT FALSE,
    share_color VARCHAR(50),
    share_layout VARCHAR(20) DEFAULT 'grid',
    
    -- Licensing
    license_price_cents INTEGER DEFAULT 0,
    license_currency VARCHAR(10) DEFAULT 'USD',
    
    -- Channel and communication
    channel_url TEXT,
    
    -- Additional metadata (JSON)
    -- Contains: descriptions, slideshow, order, system_vault
    metadata JSONB DEFAULT '{}'::jsonb,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- Create unique index on uid + vault_name
CREATE UNIQUE INDEX IF NOT EXISTS idx_vaults_uid_name ON vaults(uid, vault_name);

-- Create index on uid for faster lookups
CREATE INDEX IF NOT EXISTS idx_vaults_uid ON vaults(uid);

-- Create trigger to auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_vaults_updated_at 
    BEFORE UPDATE ON vaults 
    FOR EACH ROW 
    EXECUTE FUNCTION update_updated_at_column();

-- Verify table was created
SELECT 
    column_name, 
    data_type, 
    is_nullable,
    column_default
FROM information_schema.columns
WHERE table_name = 'vaults'
ORDER BY ordinal_position;
