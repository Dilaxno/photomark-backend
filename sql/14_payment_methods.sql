-- Payment Methods table for storing user saved payment methods
-- Run this migration to add the payment_methods table

CREATE TABLE IF NOT EXISTS payment_methods (
    id SERIAL PRIMARY KEY,
    payment_method_id VARCHAR(255) NOT NULL,
    user_uid VARCHAR(128) NOT NULL,
    type VARCHAR(50) NOT NULL DEFAULT 'card',
    last4 VARCHAR(4),
    expiry_month VARCHAR(2),
    expiry_year VARCHAR(4),
    expiry VARCHAR(10),
    brand VARCHAR(50),
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_payment_methods_user_uid ON payment_methods(user_uid);
CREATE INDEX IF NOT EXISTS idx_payment_methods_payment_method_id ON payment_methods(payment_method_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_methods_user_method ON payment_methods(user_uid, payment_method_id);

-- Comments
COMMENT ON TABLE payment_methods IS 'User saved payment methods from payment provider (Dodo Payments)';
COMMENT ON COLUMN payment_methods.payment_method_id IS 'External payment method ID from payment provider';
COMMENT ON COLUMN payment_methods.user_uid IS 'Firebase user UID';
COMMENT ON COLUMN payment_methods.type IS 'Payment method type: card, visa, mastercard, amex, paypal, etc.';
COMMENT ON COLUMN payment_methods.last4 IS 'Last 4 digits of card number';
COMMENT ON COLUMN payment_methods.expiry_month IS 'Card expiry month (MM)';
COMMENT ON COLUMN payment_methods.expiry_year IS 'Card expiry year (YYYY or YY)';
COMMENT ON COLUMN payment_methods.expiry IS 'Formatted expiry (MM/YY)';
COMMENT ON COLUMN payment_methods.brand IS 'Card brand (Visa, Mastercard, etc.)';
COMMENT ON COLUMN payment_methods.is_default IS '1 = default payment method, 0 = not default';
