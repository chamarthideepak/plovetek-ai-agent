"""Additive dashboard schema. Existing inbox and auto-send state are preserved."""
from web_database import database, initialize_database

def initialize_features():
    initialize_database()
    statements = [
        '''CREATE TABLE IF NOT EXISTS inbox (
        id TEXT PRIMARY KEY, sender TEXT NOT NULL, subject TEXT NOT NULL,
        body TEXT NOT NULL, status TEXT NOT NULL, draft TEXT)''',
        '''CREATE TABLE IF NOT EXISTS web_conversations (
        id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL,
        email_id TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES web_users(id) ON DELETE CASCADE)''',
        '''CREATE TABLE IF NOT EXISTS web_chat_leases (
        conversation_id TEXT PRIMARY KEY, lease_token TEXT NOT NULL, expires_at INTEGER NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS web_send_reviews (
        token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, email_id TEXT NOT NULL,
        draft_hash TEXT NOT NULL, metadata TEXT NOT NULL, expires_at INTEGER NOT NULL,
        used INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(user_id) REFERENCES web_users(id) ON DELETE CASCADE)''',
        '''CREATE INDEX IF NOT EXISTS idx_web_conversations_user
        ON web_conversations(user_id, created_at)''',
    ]
    with database() as db:
        for sql in statements:
            db.execute(sql)
