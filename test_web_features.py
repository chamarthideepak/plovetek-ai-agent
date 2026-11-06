"""Offline API tests with temporary data and simulated Gemini/Gmail."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import web_database
from web_auth import create_owner
from api import app

class Result:
    def __init__(self, value=None, error=None): self.value,self.error=value,error
    def execute(self, **kwargs):
        if self.error: raise self.error
        return self.value

class Gmail:
    def __init__(self, account='owner@example.test', fail=False):
        self.account,self.fail,self.sent=account,fail,[]
    def getProfile(self, **kwargs): return Result({'emailAddress':self.account})
    def messages(self): return self
    def get(self, **kwargs):
        return Result({'threadId':'thread-1','payload':{'headers':[
            {'name':'From','value':'Customer <customer@example.test>'},
            {'name':'Subject','value':'Quotation request'},
            {'name':'Message-ID','value':'<mail@example.test>'}]}})
    def send(self, **kwargs):
        self.sent.append(kwargs['body'])
        return Result({'id':'outgoing-1'},TimeoutError() if self.fail else None)

class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=web_database.DB_PATH
        web_database.DB_PATH=Path(self.tmp.name)/'demo.sqlite3'
        self.client=TestClient(app);self.client.__enter__()
        create_owner('owner@example.test','Long private test password')
        result=self.client.post('/auth/login',json={'email':'owner@example.test','password':'Long private test password'})
        self.auth={'Authorization':'Bearer '+result.json()['access_token']}
        with web_database.database() as db:
            db.executemany('INSERT INTO inbox VALUES (?,?,?,?,?,?)',[
                ('gmail-pending','customer@example.test','Quotation request','Please send pricing','pending','Hello, please share your requirements.'),
                ('gmail-refund','customer@example.test','Refund request','Return my last payment','escalated',None),
                ('gmail-sent','customer@example.test','New enquiry','Information request','sent_live','Thank you.'),
                ('newsletter-1','news@example.test','Weekly newsletter','Updates','ignored',None)])
            db.execute('CREATE TABLE mailbox_binding (id INTEGER PRIMARY KEY,account TEXT)')
            db.execute("INSERT INTO mailbox_binding VALUES (1,'owner@example.test')")
    def tearDown(self):
        self.client.__exit__(None,None,None);web_database.DB_PATH=self.old;self.tmp.cleanup()
    def get(self,path):return self.client.get(path,headers=self.auth)
    def post(self,path,data=None):return self.client.post(path,headers=self.auth,json=data)
    def preview(self,gmail):
        with patch('web_send.gmail_service',return_value=gmail):
            return self.post('/emails/gmail-pending/send-preview')
    def test_every_feature_requires_auth(self):
        for method,path,data in [('GET','/dashboard',None),('GET','/emails',None),('GET','/emails/gmail-pending',None),('PATCH','/emails/gmail-pending/draft',{'draft':'x','expected_draft':None}),('POST','/emails/gmail-pending/actions',{'action':'reject'}),('POST','/emails/gmail-pending/send-preview',None),('POST','/emails/gmail-pending/send',{'review_token':'x'*30,'confirmation':'SEND'}),('GET','/chat/conversations',None),('POST','/chat/messages',{'message':'Hi'})]:
            with self.subTest(path=path):self.assertEqual(self.client.request(method,path,json=data).status_code,401)
    def test_dashboard_and_filtered_pagination(self):
        self.assertEqual(self.get('/dashboard').json()['total'],4)
        result=self.get('/emails?status=pending&limit=1').json()
        self.assertEqual(result['total'],1);self.assertEqual(result['items'][0]['id'],'gmail-pending')
        self.assertEqual(self.get('/emails?search=refund').json()['total'],1)
        self.assertEqual(self.get('/emails?search=%27%20OR%201=1--').json()['total'],0)
        self.assertEqual(self.get('/emails?limit=9999').status_code,422)
        self.assertEqual(self.get('/emails?status=invalid').status_code,422)
    def test_email_detail_and_missing(self):
        self.assertEqual(self.get('/emails/gmail-pending').json()['body'],'Please send pricing')
        self.assertEqual(self.get('/emails/missing').status_code,404)
    def test_edit_checks_current_draft_and_status(self):
        path='/emails/gmail-pending/draft';old=self.get('/emails/gmail-pending').json()['draft']
        response=self.client.patch(path,headers=self.auth,json={'draft':'Updated reply','expected_draft':old})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.patch(path,headers=self.auth,json={'draft':'Stale reply','expected_draft':old}).status_code,409)
        self.assertEqual(self.client.patch('/emails/gmail-sent/draft',headers=self.auth,json={'draft':'x','expected_draft':'Thank you.'}).status_code,409)
    def test_reject_and_resolve_do_not_send(self):
        self.assertEqual(self.post('/emails/gmail-pending/actions',{'action':'reject'}).json()['status'],'rejected')
        self.assertEqual(self.post('/emails/gmail-refund/actions',{'action':'resolve'}).json()['status'],'resolved')
        self.assertEqual(self.post('/emails/gmail-refund/actions',{'action':'resolve'}).status_code,409)
        self.assertEqual(self.post('/emails/gmail-sent/actions',{'action':'reject'}).status_code,409)
    def test_preview_does_not_send(self):
        gmail=Gmail();response=self.preview(gmail)
        self.assertEqual(response.status_code,200);self.assertEqual(gmail.sent,[])
        self.assertEqual(response.json()['to'],'customer@example.test')
    def test_confirmed_send_is_single_attempt(self):
        gmail=Gmail();review=self.preview(gmail).json()
        with patch('web_send.gmail_service',return_value=gmail):
            payload={'review_token':review['review_token'],'confirmation':'SEND'}
            self.assertEqual(self.post('/emails/gmail-pending/send',payload).status_code,200)
            self.assertEqual(self.post('/emails/gmail-pending/send',payload).status_code,409)
        self.assertEqual(len(gmail.sent),1)
        self.assertEqual(self.get('/emails/gmail-pending').json()['status'],'sent_live')
    def test_preview_invalidated_by_draft_change(self):
        gmail=Gmail();review=self.preview(gmail).json()
        with web_database.database() as db:db.execute("UPDATE inbox SET draft='Changed elsewhere' WHERE id='gmail-pending'")
        with patch('web_send.gmail_service',return_value=gmail):
            result=self.post('/emails/gmail-pending/send',{'review_token':review['review_token'],'confirmation':'SEND'})
        self.assertEqual(result.status_code,409);self.assertEqual(gmail.sent,[])
    def test_expired_review_and_missing_confirmation(self):
        gmail=Gmail();review=self.preview(gmail).json()
        self.assertEqual(self.post('/emails/gmail-pending/send',{'review_token':review['review_token'],'confirmation':'NO'}).status_code,422)
        with web_database.database() as db:db.execute('UPDATE web_send_reviews SET expires_at=0')
        self.assertEqual(self.post('/emails/gmail-pending/send',{'review_token':review['review_token'],'confirmation':'SEND'}).status_code,409)
        self.assertEqual(gmail.sent,[])
    def test_account_mismatch_blocks_preview(self):
        gmail=Gmail(account='different@example.test')
        self.assertEqual(self.preview(gmail).status_code,409);self.assertEqual(gmail.sent,[])
    def test_uncertain_send_is_held_and_not_retried(self):
        gmail=Gmail(fail=True);review=self.preview(gmail).json()
        with patch('web_send.gmail_service',return_value=gmail):
            payload={'review_token':review['review_token'],'confirmation':'SEND'}
            self.assertEqual(self.post('/emails/gmail-pending/send',payload).status_code,502)
            self.assertEqual(self.post('/emails/gmail-pending/send',payload).status_code,409)
        self.assertEqual(len(gmail.sent),1)
        self.assertEqual(self.get('/emails/gmail-pending').json()['status'],'sending')
    def test_local_chat_and_saved_history(self):
        with patch('web_routes.generate_reply') as model:
            result=self.post('/chat/messages',{'message':'Which replies are pending?'})
            model.assert_not_called()
        data=result.json();self.assertEqual(data['source'],'local');self.assertIn('gmail-pending',data['answer'])
        history=self.get('/chat/conversations/'+data['conversation_id']).json()
        self.assertEqual(len(history['messages']),2)
        self.assertEqual(len(self.get('/chat/conversations').json()['items']),1)
    def test_ai_followup_has_scoped_history(self):
        with patch('web_routes.generate_reply',return_value='Please share requirements.') as model:
            first=self.post('/chat/messages',{'message':'Explain this email','email_id':'gmail-pending'}).json()
            self.assertEqual(model.call_args.args[3]['id'],'gmail-pending')
            second=self.post('/chat/messages',{'message':'Make it shorter','conversation_id':first['conversation_id']})
            self.assertEqual(second.status_code,200)
            self.assertEqual(len(model.call_args.args[1]),2)
        self.assertEqual(self.post('/chat/messages',{'message':'Switch email','conversation_id':first['conversation_id'],'email_id':'gmail-refund'}).status_code,409)
    def test_ai_failure_does_not_save_fake_answer(self):
        with patch('web_routes.generate_reply',side_effect=RuntimeError('secret upstream details')):
            result=self.post('/chat/messages',{'message':'Hello assistant'})
        self.assertEqual(result.status_code,503);self.assertNotIn('secret upstream',result.text)
        with web_database.database() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM web_chat_messages').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM web_chat_leases').fetchone()[0],0)
    def test_unknown_conversation_and_chat_validation(self):
        self.assertEqual(self.post('/chat/messages',{'message':'Hi','conversation_id':'00000000-0000-0000-0000-000000000000'}).status_code,404)
        self.assertEqual(self.post('/chat/messages',{'message':'  '}).status_code,422)
        self.assertEqual(self.post('/chat/messages',{'message':'x'*4001}).status_code,422)
    def test_chat_busy_claim_prevents_overlapping_turn(self):
        data=self.post('/chat/messages',{'message':'Show escalations'}).json();cid=data['conversation_id']
        with web_database.database() as db:db.execute('INSERT INTO web_chat_leases VALUES (?,?,?)',(cid,'busy',int(time.time())+120))
        self.assertEqual(self.post('/chat/messages',{'message':'Show escalations','conversation_id':cid}).status_code,409)
    def test_frontend_assets_and_audit_entries(self):
        self.assertEqual(self.client.get('/').status_code,200)
        self.assertIn('Plovetek Workspace',self.client.get('/').text)
        self.assertEqual(self.client.get('/assets/dashboard.js').status_code,200)
        self.assertEqual(self.client.get('/assets/styles.css').status_code,200)
        self.assertEqual(self.client.get('/assets/../.env').status_code,404)
        self.post('/emails/gmail-refund/actions',{'action':'resolve'})
        with web_database.database() as db:
            self.assertTrue(db.execute("SELECT 1 FROM web_audit_events WHERE action='escalation_resolved' AND user_id=1").fetchone())

if __name__=='__main__':unittest.main()
