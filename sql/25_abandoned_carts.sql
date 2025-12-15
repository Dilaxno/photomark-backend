-- Abandoned Cart Recovery Tables
-- Tracks cart sessions for abandoned cart recovery emails

CREATE TABLE IF NOT EXISTS abandoned_carts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Shop info
    shop_uid VARCHAR(128) NOT NULL,
    shop_slug VARCHAR(255),
    
    -- Customer info
    customer_email VARCHAR(255),
    customer_name VARCHAR(255),
    
    -- Session tracking
    session_id VARCHAR(128) NOT NULL,
    ip_address VARCHAR(45),
    user_agent TEXT,
    
    -- Cart contents
    items JSONB DEFAULT '[]'::jsonb,
    cart_total_cents INTEGER DEFAULT 0,
    currency VARCHAR(10) DEFAULT 'USD',
    
    -- Recovery tracking
    recovery_email_sent BOOLEAN DEFAULT FALSE,
    recovery_email_sent_at TIMESTAMP,
    recovery_email_count INTEGER DEFAULT 0,
    
    -- Conversion tracking
    converted BOOLEAN DEFAULT FALSE,
    converted_at TIMESTAMP,
    conversion_payment_id VARCHAR(128),
    
    -- Recovery link
    recovery_token VARCHAR(64) UNIQUE,
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_activity_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_abandoned_carts_shop ON abandoned_carts(shop_uid);
CREATE INDEX IF NOT EXISTS idx_abandoned_carts_email ON abandoned_carts(customer_email);
CREATE INDEX IF NOT EXISTS idx_abandoned_carts_session ON abandoned_carts(session_id);
CREATE INDEX IF NOT EXISTS idx_abandoned_carts_token ON abandoned_carts(recovery_token);
CREATE INDEX IF NOT EXISTS idx_abandoned_carts_last_activity ON abandoned_carts(last_activity_at);
CREATE INDEX IF NOT EXISTS idx_abandoned_carts_not_converted ON abandoned_carts(converted, recovery_email_sent, last_activity_at) 
    WHERE converted = FALSE;
