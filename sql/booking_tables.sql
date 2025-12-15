-- Booking System Tables
-- Run this migration to create the booking/CRM tables

-- Clients table
CREATE TABLE IF NOT EXISTS booking_clients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uid VARCHAR(128) NOT NULL,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    phone VARCHAR(50),
    company VARCHAR(255),
    address TEXT,
    city VARCHAR(100),
    state VARCHAR(100),
    zip_code VARCHAR(20),
    country VARCHAR(100),
    notes TEXT,
    tags JSONB DEFAULT '[]'::jsonb,
    source VARCHAR(100),
    referral_source VARCHAR(255),
    avatar_url TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_booking_clients_uid ON booking_clients(uid);
CREATE INDEX IF NOT EXISTS idx_booking_clients_email ON booking_clients(email);

-- Session packages table
CREATE TABLE IF NOT EXISTS booking_packages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uid VARCHAR(128) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    session_type VARCHAR(50) DEFAULT 'other',
    price FLOAT DEFAULT 0.0,
    currency VARCHAR(3) DEFAULT 'USD',
    deposit_amount FLOAT DEFAULT 0.0,
    deposit_percentage FLOAT,
    duration_minutes INTEGER DEFAULT 60,
    included_photos INTEGER,
    included_hours FLOAT,
    deliverables JSONB DEFAULT '[]'::jsonb,
    is_active BOOLEAN DEFAULT TRUE,
    color VARCHAR(7),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_booking_packages_uid ON booking_packages(uid);


-- Bookings table
CREATE TABLE IF NOT EXISTS bookings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uid VARCHAR(128) NOT NULL,
    client_id UUID REFERENCES booking_clients(id) ON DELETE SET NULL,
    client_name VARCHAR(255),
    client_email VARCHAR(255),
    client_phone VARCHAR(50),
    title VARCHAR(255),
    session_type VARCHAR(50) DEFAULT 'other',
    package_id UUID REFERENCES booking_packages(id) ON DELETE SET NULL,
    session_date TIMESTAMP,
    session_end TIMESTAMP,
    duration_minutes INTEGER DEFAULT 60,
    timezone VARCHAR(50) DEFAULT 'UTC',
    location TEXT,
    location_address TEXT,
    location_notes TEXT,
    is_virtual BOOLEAN DEFAULT FALSE,
    meeting_link TEXT,
    status VARCHAR(50) DEFAULT 'inquiry',
    total_amount FLOAT DEFAULT 0.0,
    deposit_amount FLOAT DEFAULT 0.0,
    amount_paid FLOAT DEFAULT 0.0,
    currency VARCHAR(3) DEFAULT 'USD',
    payment_status VARCHAR(50) DEFAULT 'unpaid',
    notes TEXT,
    internal_notes TEXT,
    questionnaire_data JSONB DEFAULT '{}'::jsonb,
    contract_signed BOOLEAN DEFAULT FALSE,
    contract_signed_at TIMESTAMP,
    contract_id UUID,
    reminder_sent BOOLEAN DEFAULT FALSE,
    reminder_sent_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_bookings_uid ON bookings(uid);
CREATE INDEX IF NOT EXISTS idx_bookings_session_date ON bookings(session_date);
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status);
CREATE INDEX IF NOT EXISTS idx_bookings_created_at ON bookings(created_at);

-- Booking payments table
CREATE TABLE IF NOT EXISTS booking_payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uid VARCHAR(128) NOT NULL,
    booking_id UUID NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    amount FLOAT NOT NULL,
    currency VARCHAR(3) DEFAULT 'USD',
    payment_type VARCHAR(50) DEFAULT 'payment',
    payment_method VARCHAR(50),
    status VARCHAR(50) DEFAULT 'completed',
    external_id VARCHAR(255),
    notes TEXT,
    paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_booking_payments_uid ON booking_payments(uid);
CREATE INDEX IF NOT EXISTS idx_booking_payments_booking_id ON booking_payments(booking_id);

-- Booking settings table
CREATE TABLE IF NOT EXISTS booking_settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uid VARCHAR(128) NOT NULL UNIQUE,
    business_name VARCHAR(255),
    business_email VARCHAR(255),
    business_phone VARCHAR(50),
    business_logo TEXT,
    brand_logo TEXT,
    brand_primary_color VARCHAR(7) DEFAULT '#6366f1',
    brand_secondary_color VARCHAR(7) DEFAULT '#8b5cf6',
    brand_text_color VARCHAR(7) DEFAULT '#1f2937',
    brand_background_color VARCHAR(7) DEFAULT '#ffffff',
    availability JSONB DEFAULT '{}'::jsonb,
    default_duration INTEGER DEFAULT 60,
    buffer_before INTEGER DEFAULT 15,
    buffer_after INTEGER DEFAULT 15,
    min_notice_hours INTEGER DEFAULT 24,
    max_advance_days INTEGER DEFAULT 90,
    default_currency VARCHAR(3) DEFAULT 'USD',
    default_deposit_percentage FLOAT DEFAULT 25.0,
    email_notifications BOOLEAN DEFAULT TRUE,
    sms_notifications BOOLEAN DEFAULT FALSE,
    booking_page_enabled BOOLEAN DEFAULT FALSE,
    booking_page_slug VARCHAR(100) UNIQUE,
    booking_page_title VARCHAR(255),
    booking_page_description TEXT,
    timezone VARCHAR(50) DEFAULT 'America/New_York',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_booking_settings_uid ON booking_settings(uid);


-- Booking Forms table (drag-and-drop form builder)
CREATE TABLE IF NOT EXISTS booking_forms (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uid VARCHAR(128) NOT NULL,
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100),
    description TEXT,
    title VARCHAR(255),
    subtitle TEXT,
    fields JSONB DEFAULT '[]'::jsonb,
    style JSONB DEFAULT '{}'::jsonb,
    submit_button_text VARCHAR(100) DEFAULT 'Submit',
    success_message TEXT DEFAULT 'Thank you for your submission!',
    redirect_url TEXT,
    notify_email VARCHAR(255),
    send_confirmation BOOLEAN DEFAULT TRUE,
    is_active BOOLEAN DEFAULT TRUE,
    is_published BOOLEAN DEFAULT FALSE,
    views_count INTEGER DEFAULT 0,
    submissions_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_booking_forms_uid ON booking_forms(uid);
CREATE INDEX IF NOT EXISTS idx_booking_forms_slug ON booking_forms(slug);

-- Form Submissions table
CREATE TABLE IF NOT EXISTS form_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uid VARCHAR(128) NOT NULL,
    form_id UUID NOT NULL REFERENCES booking_forms(id) ON DELETE CASCADE,
    data JSONB DEFAULT '{}'::jsonb,
    contact_name VARCHAR(255),
    contact_email VARCHAR(255),
    contact_phone VARCHAR(50),
    scheduled_date TIMESTAMP,
    scheduled_end TIMESTAMP,
    status VARCHAR(50) DEFAULT 'new',
    ip_address VARCHAR(45),
    user_agent TEXT,
    referrer TEXT,
    booking_id UUID REFERENCES bookings(id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_form_submissions_uid ON form_submissions(uid);
CREATE INDEX IF NOT EXISTS idx_form_submissions_form_id ON form_submissions(form_id);
CREATE INDEX IF NOT EXISTS idx_form_submissions_contact_email ON form_submissions(contact_email);
CREATE INDEX IF NOT EXISTS idx_form_submissions_scheduled_date ON form_submissions(scheduled_date);
CREATE INDEX IF NOT EXISTS idx_form_submissions_created_at ON form_submissions(created_at);
