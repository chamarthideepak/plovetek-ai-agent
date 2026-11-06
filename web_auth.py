"""Single-owner authentication. Gmail authorization remains separate."""
import hashlib
import hmac
import re
import secrets
import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from web_database import database, initialize_database

ITERATIONS = 600_000
SESSION_SECONDS = 8 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LIMIT = 5
security = HTTPBearer(auto_error=False)

def normalize_email(email):
    normalized = email.strip().casefold()
    if len(normalized) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', normalized):
        raise ValueError('Enter a valid owner email address.')
    return normalized

def hash_password(password):
    if not 12 <= len(password) <= 1024:
        raise ValueError('Use a password between 12 and 1024 characters.')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, ITERATIONS)
    return f'pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}'

def verify_password(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or int(rounds) != ITERATIONS:
            return False
        digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False

def token_hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()

# Equal-cost verification when no account matches. No real password is embedded.
DUMMY_HASH = hash_password(secrets.token_urlsafe(32))

def create_owner(email, password):
    email = normalize_email(email)
    password_hash = hash_password(password)
    initialize_database()
    with database() as connection:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM web_users LIMIT 1').fetchone():
            raise ValueError('An owner already exists. Setup does not overwrite accounts.')
        result = connection.execute('INSERT INTO web_users (email,password_hash) VALUES (?,?)', (email, password_hash))
        connection.execute('INSERT INTO web_audit_events (user_id,action) VALUES (?,?)', (result.lastrowid, 'owner_created'))
    return email

def record_login_attempt(request, email):
    now = int(time.time())
    host = request.client.host if request.client else 'local'
    buckets = [token_hash('ip:' + host), token_hash('email:' + email)]
    blocked = False
    with database() as connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('DELETE FROM web_login_attempts WHERE attempted_at <= ?', (now - LOGIN_WINDOW_SECONDS,))
        for bucket in buckets:
            count = connection.execute('SELECT COUNT(*) FROM web_login_attempts WHERE bucket=?', (bucket,)).fetchone()[0]
            if count >= LOGIN_LIMIT:
                blocked = True
        if not blocked:
            connection.executemany('INSERT INTO web_login_attempts (bucket,attempted_at) VALUES (?,?)', [(b, now) for b in buckets])
    if blocked:
        raise HTTPException(429, 'Too many login attempts. Try again in 15 minutes.', headers={'Retry-After': str(LOGIN_WINDOW_SECONDS)})

def login_owner(request, email, password):
    email = email.strip().casefold()
    record_login_attempt(request, email)
    with database() as connection:
        user = connection.execute('SELECT id,email,password_hash FROM web_users WHERE email=?', (email,)).fetchone()
    valid = verify_password(password, user['password_hash'] if user else DUMMY_HASH)
    if not user or not valid:
        with database() as connection:
            connection.execute('INSERT INTO web_audit_events (action) VALUES (?)', ('login_failed',))
        raise HTTPException(401, 'Incorrect email or password.', headers={'WWW-Authenticate': 'Bearer'})
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with database() as connection:
        connection.execute('DELETE FROM web_sessions WHERE expires_at <= ?', (now,))
        connection.execute('INSERT INTO web_sessions (token_hash,user_id,expires_at) VALUES (?,?,?)', (token_hash(token), user['id'], now + SESSION_SECONDS))
        connection.execute('INSERT INTO web_audit_events (user_id,action) VALUES (?,?)', (user['id'], 'login_succeeded'))
    return {'access_token': token, 'token_type': 'bearer', 'expires_in': SESSION_SECONDS, 'user': {'id': user['id'], 'email': user['email']}}

def require_owner(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)]):
    if credentials is None or credentials.scheme.casefold() != 'bearer' or not 20 <= len(credentials.credentials) <= 200:
        raise HTTPException(401, 'Sign in to continue.', headers={'WWW-Authenticate': 'Bearer'})
    digest = token_hash(credentials.credentials)
    with database() as connection:
        user = connection.execute('''SELECT u.id,u.email,s.token_hash
            FROM web_sessions s JOIN web_users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.expires_at > ?''', (digest, int(time.time()))).fetchone()
    if not user:
        raise HTTPException(401, 'Session expired or invalid. Sign in again.', headers={'WWW-Authenticate': 'Bearer'})
    return dict(user)

def logout_owner(owner):
    with database() as connection:
        connection.execute('DELETE FROM web_sessions WHERE token_hash=?', (owner['token_hash'],))
        connection.execute('INSERT INTO web_audit_events (user_id,action) VALUES (?,?)', (owner['id'], 'logout'))
