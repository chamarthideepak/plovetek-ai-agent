"""Additive web tables in the existing Plovetek SQLite database."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().with_name('demo.sqlite3')

@contextmanager
def database():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def initialize_database():
    statements = [
        '''CREATE TABLE IF NOT EXISTS web_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''',
        '''CREATE TABLE IF NOT EXISTS web_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES web_users(id) ON DELETE CASCADE)''',
        '''CREATE TABLE IF NOT EXISTS web_chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES web_users(id) ON DELETE CASCADE)''',
        '''CREATE TABLE IF NOT EXISTS web_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            email_id TEXT,
            details TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES web_users(id) ON DELETE SET NULL)''',
        '''CREATE TABLE IF NOT EXISTS web_login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket TEXT NOT NULL,
            attempted_at INTEGER NOT NULL)''',
        '''CREATE INDEX IF NOT EXISTS idx_web_chat_conversation
            ON web_chat_messages(user_id, conversation_id, id)''',
        '''CREATE INDEX IF NOT EXISTS idx_web_sessions_expiry
            ON web_sessions(expires_at)''',
        '''CREATE INDEX IF NOT EXISTS idx_web_login_attempts_bucket
            ON web_login_attempts(bucket, attempted_at)''',
    ]
    with database() as connection:
        for statement in statements:
            connection.execute(statement)

if __name__ == '__main__':
    initialize_database()
    print('Web database tables initialized successfully.')
