-- Booking Analytics Table
-- Stores aggregated booking analytics per user per day

CREATE TABLE IF NOT EXISTS booking_analytics (
    id SERIAL PRIMARY KEY,
    uid VARCHAR(128) NOT NULL,
    date DATE NOT NULL,
    total_submissions INTEGER DEFAULT 0,
    conversion_rate DECIMAL(5,2) DEFAULT 0,
    by_status JSONB DEFAULT '{}',
    by_form JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(uid, date)
);

-- Index for faster queries
CREATE INDEX IF NOT EXISTS idx_booking_analytics_uid ON booking_analytics(uid);
CREATE INDEX IF NOT EXISTS idx_booking_analytics_date ON booking_analytics(date);
CREATE INDEX IF NOT EXISTS idx_booking_analytics_uid_date ON booking_analytics(uid, date);
