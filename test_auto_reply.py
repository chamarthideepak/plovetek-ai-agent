"""Offline tests: no Google credentials, AI calls, or real sends."""
import tempfile
import unittest
from pathlib import Path
from app import open_db
from auto_reply import ACKS, activation, decision, process


class Result:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error
    def execute(self, **kwargs):
        if self.error:
            raise self.error
        return self.value


class FakeUsers:
    def __init__(self, fail=False, count=1):
        self.sent = []
        self.fail, self.count = fail, count
    def threads(self): return self
    def messages(self): return self
    def get(self, **kwargs): return Result({'messages': [{}]*self.count})
    def send(self, **kwargs):
        self.sent.append(kwargs['body'])
        return Result({'id':'out-1'}, TimeoutError() if self.fail else None)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = open_db(Path(self.tmp.name)/'test.sqlite3')
        self.since = activation(self.db, 'owner@example.com')
        self.h = {'from':'Customer <customer@example.org>', 'subject':'New enquiry', 'message-id':'<m@example.org>'}
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
    def msg(self, id='m', thread='t'):
        return {'id':id, 'threadId':thread, 'internalDate':str(self.since+1000), 'labelIds':['INBOX'], 'payload':{'headers':[{'name':k,'value':v} for k,v in self.h.items()]}}
    def insert(self, id='m'):
        with self.db:
            self.db.execute('INSERT INTO inbox VALUES (?,?,?,?,?,?)', ('gmail-'+id,'customer@example.org','New enquiry','I need help','escalated',None))
    def run_process(self, users, message=None, draft=None):
        process(self.db, users, 'owner@example.com', message or self.msg(), 'I need help', self.since, draft or (lambda s,b: ACKS[0]))
    def test_routing(self):
        cases = [('I need information','auto'),('Please quote a price','pending'),('I have a complaint','pending'),('Refund please','escalated'),('Payment issue','escalated'),('Contract review','escalated'),('Weekly newsletter','ignored')]
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(decision(self.h, body,'owner@example.com')[0],expected)
    def test_exclusions(self):
        for patch in ({'from':'no-reply@example.org'},{'auto-submitted':'auto-replied'},{'list-id':'abc'},{'from':'owner@example.com'},{'return-path':'<>'}):
            self.assertEqual(decision(self.h|patch,'Hello','owner@example.com')[0],'ignored')
    def test_ambiguous_and_existing(self):
        for patch in ({'reply-to':'other@example.org'},{'from':'a@example.org,b@example.org'},{'in-reply-to':'<x>'},{'subject':'Hello'},{'message-id':''}):
            self.assertEqual(decision(self.h|patch,'Hello','owner@example.com')[0],'escalated')
    def test_send_once_across_restart(self):
        self.insert(); users=FakeUsers(); self.run_process(users)
        self.db.close(); self.db=open_db(Path(self.tmp.name)/'test.sqlite3')
        self.run_process(users)
        self.assertEqual(len(users.sent),1)
        self.assertEqual(self.db.execute('SELECT status FROM inbox').fetchone()[0],'sent_live')
        self.assertEqual(activation(self.db,'owner@example.com'),self.since)
    def test_same_thread_claim(self):
        self.insert(); users=FakeUsers(); self.run_process(users)
        self.insert('n'); self.run_process(users,self.msg('n'))
        self.assertEqual(len(users.sent),1)
    def test_uncertain_send_held(self):
        self.insert(); users=FakeUsers(fail=True); self.run_process(users); self.run_process(users)
        self.assertEqual(len(users.sent),1)
        self.assertEqual(self.db.execute('SELECT status FROM inbox').fetchone()[0],'sending')
    def test_unapproved_ai_output(self):
        self.insert(); users=FakeUsers(); self.run_process(users,draft=lambda s,b:'We guarantee delivery tomorrow')
        self.assertEqual(users.sent,[])
        self.assertEqual(self.db.execute('SELECT status FROM inbox').fetchone()[0],'escalated')
    def test_no_old_messages(self):
        self.insert(); users=FakeUsers(); message=self.msg(); message['internalDate']=str(self.since-1)
        self.run_process(users,message); self.assertEqual(users.sent,[])
    def test_existing_thread_no_send(self):
        self.insert(); users=FakeUsers(count=2); self.run_process(users); self.assertEqual(users.sent,[])
    def test_valid_mime(self):
        import base64
        from email import message_from_bytes
        self.insert(); users=FakeUsers(); self.run_process(users)
        reply=message_from_bytes(base64.urlsafe_b64decode(users.sent[0]['raw']))
        self.assertEqual(reply['To'],'customer@example.org')
        self.assertEqual(reply['Auto-Submitted'],'auto-replied')
        self.assertEqual(reply['In-Reply-To'],'<m@example.org>')
        self.assertEqual(users.sent[0]['threadId'],'t')

if __name__ == '__main__': unittest.main()
