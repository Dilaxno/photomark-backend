-- Migration: Add CleanEnroll-style fields to booking_forms table
-- Run this on your PostgreSQL database to add the new columns

-- Form type and language
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS form_type VARCHAR(20) DEFAULT 'simple';
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS language VARCHAR(10) DEFAULT 'en';

-- Theme columns
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS theme JSONB DEFAULT '{}';
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS primary_color VARCHAR(7) DEFAULT '#4f46e5';
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS background_color VARCHAR(7) DEFAULT '#ffffff';
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS text_color VARCHAR(7) DEFAULT '#111827';
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS input_bg_color VARCHAR(7) DEFAULT '#ffffff';
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS input_border_color VARCHAR(7) DEFAULT '#d1d5db';
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS input_border_radius INTEGER DEFAULT 8;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS font_family VARCHAR(100) DEFAULT 'Inter';
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS layout_variant VARCHAR(20) DEFAULT 'card';
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS branding JSONB DEFAULT '{}';

-- Submit button customization
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS submit_button_color VARCHAR(7) DEFAULT '#3b82f6';
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS submit_button_text_color VARCHAR(7) DEFAULT '#ffffff';
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS submit_button_position VARCHAR(20) DEFAULT 'left';

-- Success/Thank you settings
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS thank_you_display VARCHAR(20) DEFAULT 'message';
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS celebration_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS redirect_enabled BOOLEAN DEFAULT FALSE;

-- Auto-reply email settings
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS auto_reply_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS auto_reply_email_field_id VARCHAR(100);
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS auto_reply_subject VARCHAR(255) DEFAULT 'Thank you for contacting us';
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS auto_reply_message_html TEXT;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS auto_reply_message_text TEXT;

-- Email validation settings
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS email_validation_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS professional_emails_only BOOLEAN DEFAULT FALSE;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS block_role_emails BOOLEAN DEFAULT FALSE;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS email_reject_bad_reputation BOOLEAN DEFAULT FALSE;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS min_domain_age_days INTEGER DEFAULT 30;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS verify_email_domain BOOLEAN DEFAULT FALSE;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS detect_gibberish_email BOOLEAN DEFAULT FALSE;

-- Bot protection & spam prevention
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS honeypot_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS time_based_check_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS min_submission_time INTEGER DEFAULT 3;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS recaptcha_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS recaptcha_site_key VARCHAR(255);

-- Duplicate prevention
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS prevent_duplicate_email BOOLEAN DEFAULT FALSE;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS prevent_duplicate_by_ip BOOLEAN DEFAULT FALSE;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS duplicate_window_hours INTEGER DEFAULT 24;

-- Geo restrictions
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS restricted_countries JSONB DEFAULT '[]';
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS allowed_countries JSONB DEFAULT '[]';

-- Password protection
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS password_protection_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);

-- GDPR & Privacy
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS gdpr_compliance_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS privacy_policy_url TEXT;
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS show_powered_by BOOLEAN DEFAULT TRUE;

-- Submission limits
ALTER TABLE booking_forms ADD COLUMN IF NOT EXISTS submission_limit INTEGER DEFAULT 0;

-- ============================================================================
-- FormSubmission table updates
-- ============================================================================
ALTER TABLE form_submissions ADD COLUMN IF NOT EXISTS country_code VARCHAR(2);
ALTER TABLE form_submissions ADD COLUMN IF NOT EXISTS spam_score FLOAT DEFAULT 0.0;
ALTER TABLE form_submissions ADD COLUMN IF NOT EXISTS is_spam BOOLEAN DEFAULT FALSE;
ALTER TABLE form_submissions ADD COLUMN IF NOT EXISTS validation_errors JSONB DEFAULT '[]';
ALTER TABLE form_submissions ADD COLUMN IF NOT EXISTS form_load_time TIMESTAMP;
ALTER TABLE form_submissions ADD COLUMN IF NOT EXISTS submission_time TIMESTAMP;
ALTER TABLE form_submissions ADD COLUMN IF NOT EXISTS time_to_complete_seconds INTEGER;

-- Done!
SELECT 'Migration completed successfully!' as status;
