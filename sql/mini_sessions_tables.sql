-- Mini Sessions Tables (UseSession.com style booking)
-- Run this migration to add mini-session support

-- Mini Sessions (the event/campaign)
CREATE TABLE IF NOT EXISTS mini_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uid VARCHAR(128) NOT NULL,
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100),
    description TEXT,
    session_type VARCHAR(50) DEFAULT 'portrait',
    duration_minutes INTEGER DEFAULT 20,
    buffer_minutes INTEGER DEFAULT 10,
    price FLOAT DEFAULT 0.0,
    deposit_amount FLOAT DEFAULT 0.0,
    currency VARCHAR(3) DEFAULT 'USD',
    included_photos INTEGER,
    deliverables JSONB DEFAULT '[]',
    location_name VARCHAR(255),
    location_address TEXT,
    location_notes TEXT,
    cover_image TEXT,
    gallery_images JSONB DEFAULT '[]',
    max_bookings_per_slot INTEGER DEFAULT 1,
    allow_waitlist BOOLEAN DEFAULT TRUE,
    require_deposit BOOLEAN DEFAULT TRUE,
    auto_confirm BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    is_published BOOLEAN DEFAULT FALSE,
    views_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mini_sessions_uid ON mini_sessions(uid);
CREATE INDEX IF NOT EXISTS idx_mini_sessions_slug ON mini_sessions(slug);


-- Mini Session Dates (specific dates for a mini-session)
CREATE TABLE IF NOT EXISTS mini_session_dates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uid VARCHAR(128) NOT NULL,
    mini_session_id UUID NOT NULL REFERENCES mini_sessions(id) ON DELETE CASCADE,
    session_date TIMESTAMP NOT NULL,
    location_name VARCHAR(255),
    location_address TEXT,
    notes TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mini_session_dates_uid ON mini_session_dates(uid);
CREATE INDEX IF NOT EXISTS idx_mini_session_dates_session ON mini_session_dates(mini_session_id);
CREATE INDEX IF NOT EXISTS idx_mini_session_dates_date ON mini_session_dates(session_date);

-- Mini Session Slots (individual time slots)
CREATE TABLE IF NOT EXISTS mini_session_slots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uid VARCHAR(128) NOT NULL,
    session_date_id UUID NOT NULL REFERENCES mini_session_dates(id) ON DELETE CASCADE,
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP NOT NULL,
    status VARCHAR(50) DEFAULT 'available',
    booking_id UUID REFERENCES bookings(id) ON DELETE SET NULL,
    held_until TIMESTAMP,
    held_by_email VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mini_session_slots_uid ON mini_session_slots(uid);
CREATE INDEX IF NOT EXISTS idx_mini_session_slots_date ON mini_session_slots(session_date_id);
CREATE INDEX IF NOT EXISTS idx_mini_session_slots_time ON mini_session_slots(start_time);
CREATE INDEX IF NOT EXISTS idx_mini_session_slots_status ON mini_session_slots(status);

-- Waitlist
CREATE TABLE IF NOT EXISTS booking_waitlist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uid VARCHAR(128) NOT NULL,
    mini_session_id UUID REFERENCES mini_sessions(id) ON DELETE CASCADE,
    session_date_id UUID REFERENCES mini_session_dates(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    phone VARCHAR(50),
    preferred_dates JSONB DEFAULT '[]',
    preferred_times JSONB DEFAULT '[]',
    notes TEXT,
    status VARCHAR(50) DEFAULT 'waiting',
    notified_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_booking_waitlist_uid ON booking_waitlist(uid);
CREATE INDEX IF NOT EXISTS idx_booking_waitlist_session ON booking_waitlist(mini_session_id);
CREATE INDEX IF NOT EXISTS idx_booking_waitlist_email ON booking_waitlist(email);

-- Add new columns to booking_settings if they don't exist
ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS business_logo TEXT;
ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS business_website TEXT;
ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS booking_page_cover_image TEXT;
ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS booking_page_theme VARCHAR(50) DEFAULT 'light';
ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS booking_page_accent_color VARCHAR(7) DEFAULT '#6366f1';
