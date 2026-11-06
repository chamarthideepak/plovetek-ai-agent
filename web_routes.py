"""Owner-only inbox, reviewed actions and persistent chat endpoints."""
import json
import secrets
import time
from typing import Annotated, Literal
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from web_auth import require_owner
from web_database import database
from web_chat import generate_reply
from web_send import preview_send, send_reviewed

Owner = Annotated[dict, Depends(require_owner)]
Status = Literal['ignored','task','pending','escalated','rejected','sent','sending','sent_live','resolved']
LABELS = {'ignored':'No reply needed','task':'Internal follow-up','pending':'Waiting for approval',
          'escalated':'Owner review required','rejected':'Draft rejected','sent':'Sent in demo',
          'sending':'Send needs verification','sent_live':'Sent through Gmail','resolved':'Owner review completed'}
router = APIRouter()

def email_record(db, email_id):
    row = db.execute('SELECT * FROM inbox WHERE id=?', (email_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Email not found.')
    result = dict(row)
    result['status_label'] = LABELS.get(row['status'], row['status'])
    result['excerpt'] = ' '.join(row['body'].split())[:220]
    return result

@router.get('/dashboard', tags=['Dashboard'])
def dashboard(owner: Owner):
    with database() as db:
        counts = {row['status']:row['n'] for row in db.execute('SELECT status,COUNT(*) n FROM inbox GROUP BY status')}
    return {'total':sum(counts.values()), 'counts':{s:counts.get(s,0) for s in LABELS},
            'scope':'Imported local emails, including demo records. Watcher runs separately.'}

@router.get('/emails', tags=['Emails'])
def emails(owner: Owner, status: Status | None = None, search: str = Query('',max_length=200),
           limit: int = Query(50,ge=1,le=100), offset: int = Query(0,ge=0)):
    where = 'WHERE (? IS NULL OR status=?) AND (? = \'\' OR instr(lower(subject || \' \' || sender),lower(?)) > 0)'
    args = (status,status,search,search)
    with database() as db:
        total = db.execute('SELECT COUNT(*) FROM inbox '+where,args).fetchone()[0]
        rows = db.execute('SELECT id,sender,subject,status,substr(body,1,220) excerpt FROM inbox '+where+' ORDER BY rowid DESC LIMIT ? OFFSET ?',args+(limit,offset)).fetchall()
    items = [dict(row)|{'status_label':LABELS.get(row['status'],row['status'])} for row in rows]
    return {'items':items,'total':total,'limit':limit,'offset':offset}

@router.get('/emails/{email_id}', tags=['Emails'])
def email_detail(email_id: str, owner: Owner):
    with database() as db:
        return email_record(db,email_id)

class DraftUpdate(BaseModel):
    draft: str = Field(min_length=1,max_length=20000)
    expected_draft: str | None = Field(...,max_length=20000)
    @field_validator('draft')
    @classmethod
    def nonblank(cls,value):
        if not value.strip(): raise ValueError('Draft cannot be blank.')
        return value.strip()

@router.patch('/emails/{email_id}/draft', tags=['Emails'])
def update_draft(email_id: str, data: DraftUpdate, owner: Owner):
    with database() as db:
        row = email_record(db,email_id)
        if row['status'] != 'pending': raise HTTPException(409,'Only pending drafts can be edited.')
        changed = db.execute("UPDATE inbox SET draft=? WHERE id=? AND status='pending' AND draft IS ?",(data.draft,email_id,data.expected_draft))
        if changed.rowcount != 1: raise HTTPException(409,'Draft changed. Reload the email before editing.')
        db.execute('INSERT INTO web_audit_events (user_id,action,email_id) VALUES (?,?,?)',(owner['id'],'draft_edited',email_id))
        return email_record(db,email_id)

class EmailAction(BaseModel):
    action: Literal['reject','resolve']

@router.post('/emails/{email_id}/actions', tags=['Emails'])
def email_action(email_id: str, data: EmailAction, owner: Owner):
    before,after = ('pending','rejected') if data.action=='reject' else ('escalated','resolved')
    with database() as db:
        email_record(db,email_id)
        changed = db.execute('UPDATE inbox SET status=? WHERE id=? AND status=?',(after,email_id,before))
        if changed.rowcount != 1: raise HTTPException(409,'Email is no longer eligible for this action.')
        db.execute('INSERT INTO web_audit_events (user_id,action,email_id) VALUES (?,?,?)',(owner['id'],'draft_rejected' if data.action=='reject' else 'escalation_resolved',email_id))
        return email_record(db,email_id)

@router.post('/emails/{email_id}/send-preview', tags=['Emails'])
def send_preview(email_id: str, owner: Owner):
    return preview_send(email_id,owner)

class SendRequest(BaseModel):
    review_token: str = Field(min_length=20,max_length=200)
    confirmation: Literal['SEND']

@router.post('/emails/{email_id}/send', tags=['Emails'])
def send_email(email_id: str, data: SendRequest, owner: Owner):
    return send_reviewed(email_id,owner,data.review_token,data.confirmation)

class ChatRequest(BaseModel):
    message: str = Field(min_length=1,max_length=4000)
    conversation_id: UUID | None = None
    email_id: str | None = Field(None,max_length=200)
    @field_validator('message')
    @classmethod
    def nonblank(cls,value):
        if not value.strip(): raise ValueError('Message cannot be blank.')
        return value.strip()

@router.get('/chat/conversations', tags=['Chat'])
def conversations(owner: Owner, limit: int = Query(50,ge=1,le=100),offset: int = Query(0,ge=0)):
    with database() as db:
        rows = db.execute('SELECT id,title,email_id,created_at FROM web_conversations WHERE user_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?', (owner['id'],limit,offset)).fetchall()
    return {'items':[dict(row) for row in rows]}

@router.get('/chat/conversations/{conversation_id}', tags=['Chat'])
def conversation_history(conversation_id: UUID, owner: Owner, after_id: int = Query(0,ge=0),limit: int = Query(100,ge=1,le=200)):
    cid = str(conversation_id)
    with database() as db:
        conv = db.execute('SELECT id,title,email_id FROM web_conversations WHERE id=? AND user_id=?',(cid,owner['id'])).fetchone()
        if not conv: raise HTTPException(404,'Conversation not found.')
        rows = db.execute('SELECT id,role,content,created_at FROM web_chat_messages WHERE user_id=? AND conversation_id=? AND id>? ORDER BY id LIMIT ?', (owner['id'],cid,after_id,limit+1)).fetchall()
    items = [dict(row) for row in rows[:limit]]
    return dict(conv)|{'messages':items,'has_more':len(rows)>limit,'next_after_id':items[-1]['id'] if items else after_id}

def queue_answer(db,message):
    words = message.lower().strip()
    if not words.startswith(('show ','list ','which ','what emails','what replies')): return None
    status = next((s for term,s in [('pending','pending'),('approval','pending'),('escalat','escalated'),('owner review','escalated'),('task','task')] if term in words),None)
    if not status: return None
    rows = db.execute('SELECT id,subject FROM inbox WHERE status=? ORDER BY rowid DESC LIMIT 51',(status,)).fetchall()
    if not rows:return 'No matching emails in the imported inbox.'
    answer = '\n'.join(f"{r['id']} | {r['subject']} | {LABELS[status]}" for r in rows[:50])
    return answer+ ('\nMore records are available in the Inbox.' if len(rows)>50 else '')

@router.post('/chat/messages', tags=['Chat'])
def chat(data: ChatRequest, owner: Owner):
    cid = str(data.conversation_id) if data.conversation_id else str(uuid4())
    lease = secrets.token_urlsafe(24)
    now = int(time.time())
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        if data.conversation_id:
            conv = db.execute('SELECT * FROM web_conversations WHERE id=? AND user_id=?',(cid,owner['id'])).fetchone()
            if not conv: raise HTTPException(404,'Conversation not found.')
            if data.email_id is not None and data.email_id != conv['email_id']:
                raise HTTPException(409,'Start a new conversation to change the selected email.')
            selected = conv['email_id']
        else:
            selected = data.email_id
            if selected: email_record(db,selected)
            db.execute('INSERT INTO web_conversations (id,user_id,title,email_id) VALUES (?,?,?,?)',(cid,owner['id'],data.message[:80],selected))
        db.execute('DELETE FROM web_chat_leases WHERE expires_at <= ?',(now,))
        claim = db.execute('INSERT OR IGNORE INTO web_chat_leases VALUES (?,?,?)',(cid,lease,now+120))
        if claim.rowcount != 1: raise HTTPException(409,'This conversation already has a response in progress.')
        history = [dict(r) for r in db.execute('SELECT role,content FROM web_chat_messages WHERE user_id=? AND conversation_id=? ORDER BY id DESC LIMIT 12',(owner['id'],cid)).fetchall()][::-1]
        counts = {r['status']:r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM inbox GROUP BY status')}
        record = email_record(db,selected) if selected else None
        local_answer = queue_answer(db,data.message) if selected is None else None
    try:
        if record:
            record = {key:record[key] for key in ('id','sender','subject','body','status_label','draft')}
            record['body'] = record['body'][:40000]
            if record['draft']:record['draft'] = record['draft'][:20000]
        try:
            answer = local_answer if local_answer is not None else generate_reply(data.message,history,counts,record)
            if not isinstance(answer,str) or not answer.strip(): raise ValueError('No answer')
        except Exception as error:
            with database() as db:
                db.execute('INSERT INTO web_audit_events (user_id,action,email_id,details) VALUES (?,?,?,?)',(owner['id'],'chat_failed',selected,type(error).__name__))
            raise HTTPException(503,'AI response unavailable. Check Gemini configuration, network and quota, then retry. No email was sent.') from None
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            claim = db.execute('SELECT lease_token,expires_at FROM web_chat_leases WHERE conversation_id=?',(cid,)).fetchone()
            session = db.execute('SELECT 1 FROM web_sessions WHERE token_hash=? AND expires_at>?',(owner['token_hash'],int(time.time()))).fetchone()
            if not session: raise HTTPException(401,'Session expired. Sign in again.')
            if not claim or claim['lease_token']!=lease or claim['expires_at']<=int(time.time()):
                raise HTTPException(409,'Conversation response expired. Reload before retrying.')
            db.executemany('INSERT INTO web_chat_messages (user_id,conversation_id,role,content) VALUES (?,?,?,?)',[(owner['id'],cid,'user',data.message),(owner['id'],cid,'assistant',answer)])
            db.execute('INSERT INTO web_audit_events (user_id,action,email_id,details) VALUES (?,?,?,?)',(owner['id'],'chat_answered',selected,json.dumps({'conversation_id':cid,'source':'local' if local_answer is not None else 'gemini'})))
        return {'conversation_id':cid,'email_id':selected,'answer':answer,'source':'local' if local_answer is not None else 'gemini'}
    finally:
        with database() as db:
            db.execute('DELETE FROM web_chat_leases WHERE conversation_id=? AND lease_token=?',(cid,lease))
