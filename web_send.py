"""Explicit owner preview and send; shares pending/sending states with the CLI."""
import base64
import hashlib
import json
import secrets
import time
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
from fastapi import HTTPException
from web_database import database

FOLDER = Path(__file__).resolve().parent

def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

def gmail_service():
    # Web requests never start an interactive Google sign-in flow.
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from gmail_send_test import SCOPES
    path = FOLDER / 'gmail_send_token.json'
    if not path.exists():
        raise HTTPException(503, 'Gmail send authorization is missing. Authorize the existing Gmail sender locally first.')
    try:
        credentials = Credentials.from_authorized_user_file(str(path), SCOPES)
        if not credentials.has_scopes(SCOPES):
            raise ValueError('Required scopes missing')
        if not credentials.valid:
            if not credentials.refresh_token:
                raise ValueError('Refresh unavailable')
            credentials.refresh(Request())
        return build('gmail', 'v1', credentials=credentials).users()
    except Exception:
        raise HTTPException(503, 'Gmail authorization is unavailable. Reauthorize the existing sender locally.') from None

def verified_metadata(users, email_id):
    account = users.getProfile(userId='me').execute()['emailAddress'].lower()
    with database() as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mailbox_binding'").fetchone()
        binding = db.execute('SELECT account FROM mailbox_binding WHERE id=1').fetchone() if table else None
    if not binding or binding['account'].lower() != account:
        raise HTTPException(409, 'Gmail account does not match the watcher database. Start the watcher with the correct owner account first.')
    original = users.messages().get(userId='me', id=email_id.removeprefix('gmail-'), format='metadata').execute()
    headers = {h['name'].lower(): h['value'] for h in original.get('payload', {}).get('headers', [])}
    recipients = getaddresses([headers.get('reply-to') or headers.get('from', '')])
    if len(recipients) != 1 or not recipients[0][1] or not headers.get('message-id') or not original.get('threadId'):
        raise HTTPException(409, 'A single recipient, original Message-ID and thread could not be verified.')
    return {'account': account, 'recipient': recipients[0][1], 'subject': headers.get('subject', ''),
            'message_id': headers['message-id'], 'references': headers.get('references', ''), 'thread_id': original['threadId']}

def preview_send(email_id, owner):
    if not email_id.startswith('gmail-'):
        raise HTTPException(409, 'Demo messages cannot be sent through Gmail.')
    with database() as db:
        row = db.execute('SELECT draft,status FROM inbox WHERE id=?', (email_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Email not found.')
    if row['status'] != 'pending' or not row['draft']:
        raise HTTPException(409, 'This email has no pending draft.')
    try:
        metadata = verified_metadata(gmail_service(), email_id)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'Gmail preview could not be loaded. No email was sent.') from None
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with database() as db:
        db.execute('DELETE FROM web_send_reviews WHERE expires_at <= ?', (now,))
        db.execute('INSERT INTO web_send_reviews VALUES (?,?,?,?,?,?,0)',
                   (digest(token), owner['id'], email_id, digest(row['draft']), json.dumps(metadata, sort_keys=True), now+600))
    return {'review_token': token, 'from': metadata['account'], 'to': metadata['recipient'],
            'subject': metadata['subject'], 'draft': row['draft'], 'expires_in': 600}

def send_reviewed(email_id, owner, review_token, confirmation):
    if confirmation != 'SEND':
        raise HTTPException(400, 'Type SEND to confirm the displayed reply.')
    with database() as db:
        review = db.execute('SELECT * FROM web_send_reviews WHERE token_hash=? AND user_id=? AND email_id=?',
                            (digest(review_token), owner['id'], email_id)).fetchone()
    if not review or review['used'] or review['expires_at'] <= int(time.time()):
        raise HTTPException(409, 'Review expired or already used. Open a fresh preview.')
    try:
        users = gmail_service()
        metadata = verified_metadata(users, email_id)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'Gmail verification failed. No send was attempted.') from None
    if json.dumps(metadata, sort_keys=True) != review['metadata']:
        raise HTTPException(409, 'Reply metadata changed. Open a fresh preview.')
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT * FROM web_send_reviews WHERE token_hash=?', (digest(review_token),)).fetchone()
        row = db.execute('SELECT draft,status FROM inbox WHERE id=?', (email_id,)).fetchone()
        session = db.execute('SELECT 1 FROM web_sessions WHERE token_hash=? AND expires_at>?',
                             (owner['token_hash'], int(time.time()))).fetchone()
        if not session:
            raise HTTPException(401, 'Session expired. Sign in again.')
        if not current or current['used'] or current['expires_at'] <= int(time.time()):
            raise HTTPException(409, 'Review expired or already used.')
        if not row or row['status'] != 'pending' or not row['draft'] or digest(row['draft']) != current['draft_hash']:
            raise HTTPException(409, 'Draft changed or was already processed. Open a fresh preview.')
        draft = row['draft']
        # Build MIME before claiming, so local formatting failures cannot claim delivery.
        try:
            reply = EmailMessage()
            reply['From'] = metadata['account']; reply['To'] = metadata['recipient']
            reply['Subject'] = metadata['subject']; reply['In-Reply-To'] = metadata['message_id']
            reply['References'] = (metadata['references']+' '+metadata['message_id']).strip()
            reply.set_content(draft)
            raw = base64.urlsafe_b64encode(reply.as_bytes()).decode('ascii')
        except Exception:
            raise HTTPException(409, 'Reply could not be formatted. No email was sent.') from None
        db.execute("UPDATE inbox SET status='sending' WHERE id=? AND status='pending'", (email_id,))
        db.execute('UPDATE web_send_reviews SET used=1 WHERE token_hash=?', (digest(review_token),))
        db.execute('INSERT INTO web_audit_events (user_id,action,email_id,details) VALUES (?,?,?,?)',
                   (owner['id'], 'email_send_requested', email_id, json.dumps({'draft_hash': digest(draft), 'recipient': metadata['recipient']})))
    try:
        sent = users.messages().send(userId='me', body={'threadId': metadata['thread_id'], 'raw': raw}).execute(num_retries=0)
        gmail_id = sent['id']
    except Exception as error:
        with database() as db:
            db.execute('INSERT INTO web_audit_events (user_id,action,email_id,details) VALUES (?,?,?,?)',
                       (owner['id'], 'email_send_uncertain', email_id, type(error).__name__))
        raise HTTPException(502, 'Send could not be confirmed. Check Gmail Sent before any further action. This message is held to prevent a duplicate.') from None
    try:
        with database() as db:
            db.execute("UPDATE inbox SET status='sent_live' WHERE id=? AND status='sending'", (email_id,))
            db.execute('INSERT INTO web_audit_events (user_id,action,email_id,details) VALUES (?,?,?,?)',
                       (owner['id'], 'email_sent', email_id, json.dumps({'gmail_id': gmail_id})))
    except Exception:
        raise HTTPException(503, 'Gmail accepted the reply, but local status could not be updated. Check Gmail Sent; do not resend.') from None
    return {'message': 'Reply sent through Gmail.', 'gmail_message_id': gmail_id}
