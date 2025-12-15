-- Client Portal Tables
-- Allows photographer's clients to have accounts and view their galleries

-- Client accounts table
CREATE TABLE IF NOT EXISTS client_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    photographer_uid VARCHAR(128) NOT NULL,
    
    -- Client info
    email VARCHAR(255) NOT NULL,
    name VARCHAR(255),
    phone VARCHAR(50),
    
    -- Authentication
    password_hash VARCHAR(255),
    magic_link_token VARCHAR(255),
    magic_link_expires TIMESTAMP,
    
    -- Status
    is_active BOOLEAN DEFAULT TRUE,
    email_verified BOOLEAN DEFAULT FALSE,
    
    -- Metadata
    avatar_url TEXT,
    notes TEXT,
    tags JSONB DEFAULT '[]'::jsonb,
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_client_accounts_photographer ON client_accounts(photographer_uid);
CREATE INDEX IF NOT EXISTS idx_client_accounts_email ON client_accounts(email);
CREATE INDEX IF NOT EXISTS idx_client_accounts_magic_link ON client_accounts(magic_link_token);
CREATE UNIQUE INDEX IF NOT EXISTS idx_client_accounts_unique_email_per_photographer ON client_accounts(photographer_uid, email);

-- Client gallery access table
CREATE TABLE IF NOT EXISTS client_gallery_access (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES client_accounts(id) ON DELETE CASCADE,
    photographer_uid VARCHAR(128) NOT NULL,
    
    -- Gallery/Vault reference
    vault_name VARCHAR(255) NOT NULL,
    share_token VARCHAR(255),
    
    -- Access settings
    can_download BOOLEAN DEFAULT TRUE,
    can_favorite BOOLEAN DEFAULT TRUE,
    can_comment BOOLEAN DEFAULT TRUE,
    
    -- Display
    display_name VARCHAR(255),
    cover_image_url TEXT,
    
    -- Timestamps
    granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    last_viewed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_client_gallery_access_client ON client_gallery_access(client_id);
CREATE INDEX IF NOT EXISTS idx_client_gallery_access_photographer ON client_gallery_access(photographer_uid);
CREATE INDEX IF NOT EXISTS idx_client_gallery_access_share_token ON client_gallery_access(share_token);

-- Client downloads table
CREATE TABLE IF NOT EXISTS client_downloads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES client_accounts(id) ON DELETE CASCADE,
    photographer_uid VARCHAR(128) NOT NULL,
    
    -- What was downloaded
    vault_name VARCHAR(255) NOT NULL,
    photo_key VARCHAR(512),
    download_type VARCHAR(50) DEFAULT 'single',
    
    -- Download details
    file_size_bytes INTEGER,
    ip_address VARCHAR(45),
    user_agent TEXT,
    
    -- Timestamps
    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_client_downloads_client ON client_downloads(client_id);
CREATE INDEX IF NOT EXISTS idx_client_downloads_photographer ON client_downloads(photographer_uid);
CREATE INDEX IF NOT EXISTS idx_client_downloads_date ON client_downloads(downloaded_at);

-- Client purchases table
CREATE TABLE IF NOT EXISTS client_purchases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID REFERENCES client_accounts(id) ON DELETE SET NULL,
    client_email VARCHAR(255) NOT NULL,
    photographer_uid VARCHAR(128) NOT NULL,
    
    -- Purchase details
    vault_name VARCHAR(255),
    share_token VARCHAR(255),
    purchase_type VARCHAR(50) DEFAULT 'license',
    
    -- Payment info
    amount_cents INTEGER DEFAULT 0,
    currency VARCHAR(3) DEFAULT 'USD',
    payment_provider VARCHAR(50),
    payment_id VARCHAR(255),
    
    -- Status
    status VARCHAR(50) DEFAULT 'completed',
    
    -- Timestamps
    purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_client_purchases_client ON client_purchases(client_id);
CREATE INDEX IF NOT EXISTS idx_client_purchases_email ON client_purchases(client_email);
CREATE INDEX IF NOT EXISTS idx_client_purchases_photographer ON client_purchases(photographer_uid);
CREATE INDEX IF NOT EXISTS idx_client_purchases_date ON client_purchases(purchased_at);
