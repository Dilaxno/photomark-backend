-- Add last_login_ip column to users table for security notifications
-- This tracks the IP address of the user's last login to detect new device/location logins

ALTER TABLE public.users 
ADD COLUMN IF NOT EXISTS last_login_ip VARCHAR(45);

-- IPv6 addresses can be up to 45 characters (e.g., 2001:0db8:85a3:0000:0000:8a2e:0370:7334)

COMMENT ON COLUMN public.users.last_login_ip IS 'IP address of the last login, used for security notifications when login from new IP detected';
