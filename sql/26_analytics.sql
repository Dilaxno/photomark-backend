-- Analytics Tables
-- Tracks photo views, client behavior, and engagement

-- Individual photo views
CREATE TABLE IF NOT EXISTS photo_views (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_uid VARCHAR(128) NOT NULL,
    photo_key VARCHAR(512) NOT NULL,
    vault_name VARCHAR(255),
    share_token VARCHAR(255),
    visitor_hash VARCHAR(64) NOT NULL,
    ip_address VARCHAR(45),
    country VARCHAR(2),
    city VARCHAR(100),
    device_type VARCHAR(20),
    browser VARCHAR(50),
    os VARCHAR(50),
    view_duration_seconds INTEGER,
    referrer TEXT,
    source VARCHAR(50),
    viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_photo_views_owner ON photo_views(owner_uid);
CREATE INDEX IF NOT EXISTS idx_photo_views_photo ON photo_views(photo_key);
CREATE INDEX IF NOT EXISTS idx_photo_views_vault ON photo_views(vault_name);
CREATE INDEX IF NOT EXISTS idx_photo_views_date ON photo_views(viewed_at);
CREATE INDEX IF NOT EXISTS idx_photo_views_visitor ON photo_views(visitor_hash);

-- Gallery/vault page views
CREATE TABLE IF NOT EXISTS gallery_views (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_uid VARCHAR(128) NOT NULL,
    vault_name VARCHAR(255),
    share_token VARCHAR(255),
    page_type VARCHAR(50),
    visitor_hash VARCHAR(64) NOT NULL,
    ip_address VARCHAR(45),
    country VARCHAR(2),
    city VARCHAR(100),
    device_type VARCHAR(20),
    browser VARCHAR(50),
    os VARCHAR(50),
    session_id VARCHAR(128),
    session_duration_seconds INTEGER,
    photos_viewed INTEGER DEFAULT 0,
    favorited_count INTEGER DEFAULT 0,
    downloaded_count INTEGER DEFAULT 0,
    referrer TEXT,
    source VARCHAR(50),
    viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_gallery_views_owner ON gallery_views(owner_uid);
CREATE INDEX IF NOT EXISTS idx_gallery_views_vault ON gallery_views(vault_name);
CREATE INDEX IF NOT EXISTS idx_gallery_views_date ON gallery_views(viewed_at);
CREATE INDEX IF NOT EXISTS idx_gallery_views_session ON gallery_views(session_id);

-- Aggregated daily analytics
CREATE TABLE IF NOT EXISTS daily_analytics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_uid VARCHAR(128) NOT NULL,
    vault_name VARCHAR(255),
    page_type VARCHAR(50),
    date DATE NOT NULL,
    total_views INTEGER DEFAULT 0,
    unique_visitors INTEGER DEFAULT 0,
    photo_views INTEGER DEFAULT 0,
    avg_session_duration FLOAT DEFAULT 0,
    avg_photos_viewed FLOAT DEFAULT 0,
    bounce_rate FLOAT DEFAULT 0,
    favorites_count INTEGER DEFAULT 0,
    downloads_count INTEGER DEFAULT 0,
    shares_count INTEGER DEFAULT 0,
    device_breakdown JSONB DEFAULT '{}'::jsonb,
    geo_breakdown JSONB DEFAULT '{}'::jsonb,
    source_breakdown JSONB DEFAULT '{}'::jsonb,
    top_photos JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_daily_analytics_owner ON daily_analytics(owner_uid);
CREATE INDEX IF NOT EXISTS idx_daily_analytics_vault ON daily_analytics(vault_name);
CREATE INDEX IF NOT EXISTS idx_daily_analytics_date ON daily_analytics(date);
CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_analytics_unique ON daily_analytics(owner_uid, vault_name, page_type, date);

-- Per-photo analytics summary
CREATE TABLE IF NOT EXISTS photo_analytics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_uid VARCHAR(128) NOT NULL,
    photo_key VARCHAR(512) NOT NULL,
    vault_name VARCHAR(255),
    total_views INTEGER DEFAULT 0,
    unique_viewers INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0,
    downloads_count INTEGER DEFAULT 0,
    shares_count INTEGER DEFAULT 0,
    avg_view_duration FLOAT DEFAULT 0,
    first_viewed_at TIMESTAMP,
    last_viewed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_photo_analytics_owner ON photo_analytics(owner_uid);
CREATE INDEX IF NOT EXISTS idx_photo_analytics_photo ON photo_analytics(photo_key);
CREATE INDEX IF NOT EXISTS idx_photo_analytics_vault ON photo_analytics(vault_name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_photo_analytics_unique ON photo_analytics(owner_uid, photo_key);
