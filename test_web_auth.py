"""Offline tests use temporary databases; no Gmail or Gemini connection."""
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
import web_database
from web_auth import create_owner, hash_password, verify_password
from api import app

class OwnerAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = web_database.DB_PATH
        web_database.DB_PATH = Path(self.tmp.name) / 'demo.sqlite3'
        self.client = TestClient(app)
        self.client.__enter__()
        create_owner('Owner@example.com', 'Long private password 123')
    def tearDown(self):
        self.client.__exit__(None, None, None)
        web_database.DB_PATH = self.original
        self.tmp.cleanup()
    def login(self):
        return self.client.post('/auth/login', json={'email': 'OWNER@example.com', 'password': 'Long private password 123'})
    def test_login_and_protected_profile(self):
        self.assertEqual(self.client.get('/auth/me').status_code, 401)
        response = self.login()
        self.assertEqual(response.status_code, 200)
        token = response.json()['access_token']
        profile = self.client.get('/auth/me', headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(profile.json(), {'id': 1, 'email': 'owner@example.com'})
        with web_database.database() as db:
            row = db.execute('SELECT * FROM web_sessions').fetchone()
            self.assertNotEqual(row['token_hash'], token)
        self.assertNotIn('password_hash', profile.text)
        self.assertNotIn('token_hash', profile.text)
    def test_logout_revokes_token(self):
        token = self.login().json()['access_token']
        headers = {'Authorization': 'Bearer ' + token}
        self.assertEqual(self.client.post('/auth/logout', headers=headers).status_code, 200)
        self.assertEqual(self.client.get('/auth/me', headers=headers).status_code, 401)
    def test_expired_session_and_fake_token_rejected(self):
        token = self.login().json()['access_token']
        with web_database.database() as db:
            db.execute('UPDATE web_sessions SET expires_at=0')
        for value in (token, 'a' * 43):
            self.assertEqual(self.client.get('/auth/me', headers={'Authorization': 'Bearer ' + value}).status_code, 401)
    def test_bad_credentials_and_rate_limit(self):
        for _ in range(5):
            response = self.client.post('/auth/login', json={'email': 'owner@example.com', 'password': 'wrong'})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.login().status_code, 429)
    def test_no_account_enumeration(self):
        for email in ('unknown@example.com', 'owner@example.com'):
            response = self.client.post('/auth/login', json={'email': email, 'password': 'wrong'})
            self.assertEqual(response.json()['detail'], 'Incorrect email or password.')
    def test_second_setup_blocked(self):
        with self.assertRaises(ValueError):
            create_owner('second@example.com', 'Another long password 123')
    def test_password_hashing_and_policy(self):
        a = hash_password('Long private password 123')
        b = hash_password('Long private password 123')
        self.assertNotEqual(a, b)
        self.assertTrue(verify_password('Long private password 123', a))
        self.assertFalse(verify_password('incorrect', a))
        with self.assertRaises(ValueError):
            hash_password('short')
    def test_additive_schema_preserves_email_history(self):
        with web_database.database() as db:
            db.execute('CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY, sender TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, draft TEXT)')
            db.execute("INSERT INTO inbox (id,sender,subject,body,status) VALUES ('gmail-old','test@example.test','Old test','Previously processed','sent_live')")
            db.execute('CREATE TABLE auto_claims (account TEXT, thread TEXT, state TEXT)')
            db.execute("INSERT INTO auto_claims VALUES ('owner@example.com','thread-old','sent')")
        web_database.initialize_database()
        with web_database.database() as db:
            self.assertEqual(db.execute('SELECT status FROM inbox').fetchone()[0], 'sent_live')
            self.assertEqual(db.execute('SELECT state FROM auto_claims').fetchone()[0], 'sent')
    def test_login_events_and_no_public_registration(self):
        self.login()
        with web_database.database() as db:
            events = [r['action'] for r in db.execute('SELECT action FROM web_audit_events')]
        self.assertIn('owner_created', events)
        self.assertIn('login_succeeded', events)
        self.assertEqual(self.client.post('/auth/register', json={}).status_code, 404)
    def test_request_limits_and_cache(self):
        self.assertEqual(self.client.post('/auth/login', json={'email': 'x', 'password': ''}).status_code, 422)
        self.assertEqual(self.login().headers['cache-control'], 'no-store')

if __name__ == '__main__':
    unittest.main()
