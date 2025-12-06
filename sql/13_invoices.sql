-- Invoices table for storing user billing history
-- Run this migration to add the invoices table

CREATE TABLE IF NOT EXISTS invoices (
    id SERIAL PRIMARY KEY,
    invoice_id VARCHAR(255) NOT NULL UNIQUE,
    user_uid VARCHAR(128) NOT NULL,
    payment_id VARCHAR(255),
    subscription_id VARCHAR(255),
    amount NUMERIC(10, 2) NOT NULL DEFAULT 0,
    currency VARCHAR(10) NOT NULL DEFAULT 'USD',
    status VARCHAR(50) NOT NULL DEFAULT 'paid',
    plan VARCHAR(50),
    plan_display VARCHAR(255),
    billing_cycle VARCHAR(20),
    download_url TEXT,
    invoice_date TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_invoices_user_uid ON invoices(user_uid);
CREATE INDEX IF NOT EXISTS idx_invoices_invoice_id ON invoices(invoice_id);
CREATE INDEX IF NOT EXISTS idx_invoices_invoice_date ON invoices(invoice_date DESC);

-- Comments
COMMENT ON TABLE invoices IS 'User billing invoices from payment provider (Dodo Payments)';
COMMENT ON COLUMN invoices.invoice_id IS 'External invoice/payment ID from payment provider';
COMMENT ON COLUMN invoices.user_uid IS 'Firebase user UID';
COMMENT ON COLUMN invoices.payment_id IS 'Payment provider payment ID';
COMMENT ON COLUMN invoices.subscription_id IS 'Associated subscription ID';
COMMENT ON COLUMN invoices.amount IS 'Invoice amount in dollars';
COMMENT ON COLUMN invoices.currency IS 'Currency code (USD, EUR, etc.)';
COMMENT ON COLUMN invoices.status IS 'Invoice status: paid, pending, failed, refunded';
COMMENT ON COLUMN invoices.plan IS 'Plan slug (individual, studios, golden)';
COMMENT ON COLUMN invoices.plan_display IS 'Human-readable plan name';
COMMENT ON COLUMN invoices.billing_cycle IS 'Billing cycle: monthly, yearly';
COMMENT ON COLUMN invoices.download_url IS 'Invoice/receipt URL from payment provider';
