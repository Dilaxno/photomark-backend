-- Chat messages table for owner-collaborator communication
-- Secured by owner_uid to ensure only authorized users can access

CREATE TABLE IF NOT EXISTS chat_messages (
    id VARCHAR(64) PRIMARY KEY,
    owner_uid VARCHAR(128) NOT NULL,
    channel_id VARCHAR(128) NOT NULL,
    sender_id VARCHAR(128) NOT NULL,
    sender_name VARCHAR(255),
    sender_image VARCHAR(512),
    text TEXT,
    attachments JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

-- Indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_chat_messages_owner_uid ON chat_messages(owner_uid);
CREATE INDEX IF NOT EXISTS idx_chat_messages_channel_id ON chat_messages(channel_id);
CREATE INDEX IF NOT EXISTS idx_chat_messages_sender_id ON chat_messages(sender_id);
CREATE INDEX IF NOT EXISTS idx_chat_messages_created_at ON chat_messages(created_at DESC);

-- Composite index for fetching messages by channel with ordering
CREATE INDEX IF NOT EXISTS idx_chat_messages_channel_created ON chat_messages(channel_id, created_at DESC);

-- Comments
COMMENT ON TABLE chat_messages IS 'Stores chat messages between owner and collaborators';
COMMENT ON COLUMN chat_messages.owner_uid IS 'Owner UID who owns this chat channel - used for access control';
COMMENT ON COLUMN chat_messages.channel_id IS 'Channel ID format: collab_{owner_uid}';
COMMENT ON COLUMN chat_messages.sender_id IS 'User ID of the message sender';
COMMENT ON COLUMN chat_messages.attachments IS 'JSON array of attachment metadata (type, url, title, etc)';
