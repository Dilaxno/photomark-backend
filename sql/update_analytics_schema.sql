-- Update analytics tables to support enhanced tracking with device fingerprinting and download analytics

-- Update photo_views table
ALTER TABLE photo_views 
ADD COLUMN IF NOT EXISTS ip_hash VARCHAR(64),
ADD COLUMN IF NOT EXISTS device_fingerprint VARCHAR(128),
ADD COLUMN IF NOT EXISTS browser_version VARCHAR(20),
ADD COLUMN IF NOT EXISTS os_version VARCHAR(20),
ADD COLUMN IF NOT EXISTS screen_resolution VARCHAR(20),
ADD COLUMN IF NOT EXISTS is_download BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS download_type VARCHAR(20);

-- Update gallery_views table
ALTER TABLE gallery_views 
ADD COLUMN IF NOT EXISTS ip_hash VARCHAR(64),
ADD COLUMN IF NOT EXISTS device_fingerprint VARCHAR(128),
ADD COLUMN IF NOT EXISTS browser_version VARCHAR(20),
ADD COLUMN IF NOT EXISTS os_version VARCHAR(20),
ADD COLUMN IF NOT EXISTS screen_resolution VARCHAR(20),
ADD COLUMN IF NOT EXISTS photos_downloaded INTEGER DEFAULT 0;

-- Create download_events table
CREATE TABLE IF NOT EXISTS download_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_uid VARCHAR(128) NOT NULL,
    vault_name VARCHAR(255),
    share_token VARCHAR(255),
    download_type VARCHAR(20) NOT NULL,
    photo_keys JSONB,
    file_count INTEGER DEFAULT 1,
    total_size_bytes INTEGER,
    visitor_hash VARCHAR(64) NOT NULL,
    ip_hash VARCHAR(64),
    device_fingerprint VARCHAR(128),
    country VARCHAR(2),
    city VARCHAR(100),
    device_type VARCHAR(20),
    browser VARCHAR(50),
    browser_version VARCHAR(20),
    os VARCHAR(50),
    os_version VARCHAR(20),
    screen_resolution VARCHAR(20),
    is_paid BOOLEAN DEFAULT FALSE,
    payment_amount_cents INTEGER,
    payment_id VARCHAR(255),
    referrer TEXT,
    source VARCHAR(50),
    downloaded_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create indexes for download_events
CREATE INDEX IF NOT EXISTS idx_download_events_owner ON download_events(owner_uid);
CREATE INDEX IF NOT EXISTS idx_download_events_vault ON download_events(vault_name);
CREATE INDEX IF NOT EXISTS idx_download_events_token ON download_events(share_token);
CREATE INDEX IF NOT EXISTS idx_download_events_visitor ON download_events(visitor_hash);
CREATE INDEX IF NOT EXISTS idx_download_events_date ON download_events(downloaded_at);
CREATE INDEX IF NOT EXISTS idx_download_events_type ON download_events(download_type);

-- Add indexes for new photo_views columns
CREATE INDEX IF NOT EXISTS idx_photo_views_ip_hash ON photo_views(ip_hash);
CREATE INDEX IF NOT EXISTS idx_photo_views_device_fingerprint ON photo_views(device_fingerprint);
CREATE INDEX IF NOT EXISTS idx_photo_views_is_download ON photo_views(is_download);

-- Add indexes for new gallery_views columns  
CREATE INDEX IF NOT EXISTS idx_gallery_views_ip_hash ON gallery_views(ip_hash);
CREATE INDEX IF NOT EXISTS idx_gallery_views_device_fingerprint ON gallery_views(device_fingerprint);

-- Update photo_analytics table to include download tracking
ALTER TABLE photo_analytics 
ADD COLUMN IF NOT EXISTS last_downloaded_at TIMESTAMP WITH TIME ZONE;

-- Add comments for documentation
COMMENT ON TABLE download_events IS 'Tracks all download events with enhanced device fingerprinting and analytics';
COMMENT ON COLUMN download_events.visitor_hash IS 'Hash of IP + User-Agent + Device fingerprint for unique visitor tracking';
COMMENT ON COLUMN download_events.ip_hash IS 'Hashed IP address for privacy-compliant tracking';
COMMENT ON COLUMN download_events.device_fingerprint IS 'Browser fingerprint hash for device identification';
COMMENT ON COLUMN download_events.photo_keys IS 'JSON array of photo keys included in download';
COMMENT ON COLUMN download_events.total_size_bytes IS 'Total size of downloaded files in bytes';

-- Create function to update daily analytics with download data
CREATE OR REPLACE FUNCTION update_daily_analytics_downloads()
RETURNS TRIGGER AS $$
BEGIN
    -- Update daily analytics when download event is inserted
    INSERT INTO daily_analytics (
        owner_uid, vault_name, page_type, date, downloads_count
    ) VALUES (
        NEW.owner_uid, NEW.vault_name, 'vault', DATE(NEW.downloaded_at), 1
    )
    ON CONFLICT (owner_uid, vault_name, page_type, date)
    DO UPDATE SET 
        downloads_count = daily_analytics.downloads_count + 1,
        updated_at = NOW();
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create trigger for automatic daily analytics updates
DROP TRIGGER IF EXISTS trigger_update_daily_analytics_downloads ON download_events;
CREATE TRIGGER trigger_update_daily_analytics_downloads
    AFTER INSERT ON download_events
    FOR EACH ROW
    EXECUTE FUNCTION update_daily_analytics_downloads();