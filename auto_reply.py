"""Conservative, opt-in acknowledgements. No free-form AI text is auto-sent."""
import base64
import re
import time
from email.message import EmailMessage
from email.utils import getaddresses

ACKS = (
    "Hello,\n\nThank you for your enquiry. We have received your message.\n\nBest regards,\nPlovetek",
    "Hello,\n\nThank you for your enquiry. We have received your message. If you have not already shared your requirements, please send a brief description of what you need.\n\nBest regards,\nPlovetek",
)
RISK = re.compile(r'quot|pricing|price|cost|budget|complaint|unhappy|dissatisf|refund|pay|invoice|bill|contract|agreement|legal|commit|guarantee|deadline|urgent|security|password|sign.in|cancel|order|booking|purchase|unsubscribe|newsletter', re.I)


def headers_of(message):
    return {h['name'].lower(): h['value'] for h in message.get('payload', {}).get('headers', [])}


def decision(headers, body, account, labels=()):
    """Return route and reason; uncertainty never grants auto-send permission."""
    subject = headers.get('subject', '')
    content = subject + ' ' + body
    if set(labels) & {'SENT', 'DRAFT', 'SPAM', 'TRASH'}:
        return 'ignored', 'Excluded Gmail label'
    if (headers.get('auto-submitted', 'no').lower() != 'no'
        or any(k in headers for k in ('list-id', 'list-unsubscribe', 'x-auto-response-suppress'))
        or headers.get('precedence', '').lower() in ('bulk', 'list', 'junk')
        or headers.get('return-path', '').strip() == '<>'):
        return 'ignored', 'Automated or mailing-list message'
    senders = getaddresses([headers.get('from', '')])
    recipients = getaddresses([headers.get('reply-to') or headers.get('from', '')])
    if len(senders) != 1 or len(recipients) != 1:
        return 'escalated', 'Ambiguous sender'
    sender, target = senders[0][1].lower(), recipients[0][1].lower()
    if any(re.search(r'no.?reply|do.?not.?reply|mailer.daemon|postmaster', a) for a in (sender, target)):
        return 'ignored', 'Non-reply address'
    if sender == account.lower() or target == account.lower():
        return 'ignored', 'Own mailbox'
    if not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', target) or target != sender:
        return 'escalated', 'Reply address needs review'
    if re.search(r'refund|payment|contract|invoice|security|password|agreement', content, re.I):
        return 'escalated', 'Consequential request'
    if re.search(r'newsletter|unsubscribe|automated notification|delivery receipt', content, re.I):
        return 'ignored', 'Informational notification'
    if RISK.search(content):
        return 'pending', 'Business response needs approval'
    if (headers.get('in-reply-to') or headers.get('references')
        or re.match(r'\s*(re|fw|fwd)\s*:', subject, re.I)):
        return 'escalated', 'Existing conversation'
    if not headers.get('message-id') or not body.strip():
        return 'escalated', 'Missing message metadata or plain text'
    if not re.search(r'\b(enquiry|inquiry|enquiries|inquiries)\b', subject, re.I):
        return 'escalated', 'Not an explicit new enquiry'
    return 'auto', 'New-enquiry acknowledgement'


def activation(db, account):
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS auto_activation (account TEXT PRIMARY KEY, since_ms INTEGER NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS auto_claims (account TEXT, thread TEXT, local_id TEXT, state TEXT, reason TEXT, gmail_id TEXT, PRIMARY KEY(account, thread))')
        db.execute('INSERT OR IGNORE INTO auto_activation VALUES (?, ?)', (account.lower(), int(time.time()*1000)))
    return db.execute('SELECT since_ms FROM auto_activation WHERE account=?', (account.lower(),)).fetchone()[0]


def process(db, users, account, message, body, since_ms, draft_fn=None):
    account = account.lower()
    local_id = 'gmail-' + message['id']
    headers = headers_of(message)
    route, reason = decision(headers, body, account, message.get('labelIds', []))
    row = db.execute('SELECT * FROM inbox WHERE id=?', (local_id,)).fetchone()
    if not row or row['status'] not in ('pending', 'escalated', 'ignored'):
        return
    if route != 'auto':
        # Never downgrade an already consequential classification.
        if route == 'ignored' or route == 'escalated':
            with db:
                db.execute('UPDATE inbox SET status=?, draft=NULL WHERE id=? AND status IN (\'pending\',\'escalated\',\'ignored\')', (route, local_id))
        elif row['status'] != 'pending':
            from ai_drafting import prepare_draft
            try:
                draft = prepare_draft(headers.get('subject', ''), body)
                with db:
                    db.execute("UPDATE inbox SET status='pending',draft=? WHERE id=? AND status='escalated'", (draft, local_id))
            except Exception:
                pass
        return
    if int(message.get('internalDate', 0)) <= since_ms:
        return
    thread_id = message.get('threadId')
    if not thread_id:
        return
    thread = users.threads().get(userId='me', id=thread_id, format='minimal').execute()
    if len(thread.get('messages', [])) != 1:
        return
    with db:
        claimed = db.execute('INSERT OR IGNORE INTO auto_claims VALUES (?,?,?, ?,?,NULL)', (account, thread_id, local_id, 'preparing', reason))
    if claimed.rowcount != 1:
        return
    try:
        if draft_fn is None:
            from ai_drafting import prepare_acknowledgement
            draft_fn = prepare_acknowledgement
        draft = draft_fn(headers.get('subject', ''), body)
        if draft not in ACKS:
            raise ValueError('Unapproved acknowledgement')
        reply = EmailMessage()
        reply['From'] = account
        reply['To'] = getaddresses([headers['from']])[0][1]
        reply['Subject'] = headers.get('subject', '')
        reply['In-Reply-To'] = headers['message-id']
        reply['References'] = headers['message-id']
        reply['Auto-Submitted'] = 'auto-replied'
        reply['X-Auto-Response-Suppress'] = 'All'
        reply.set_content(draft)
        with db:
            claimed = db.execute("UPDATE inbox SET status='sending', draft=? WHERE id=? AND status='escalated'", (draft, local_id))
            db.execute("UPDATE auto_claims SET state='sending' WHERE account=? AND thread=?", (account, thread_id))
        if claimed.rowcount != 1:
            return
        sent = users.messages().send(userId='me', body={'threadId': thread_id, 'raw': base64.urlsafe_b64encode(reply.as_bytes()).decode('ascii')}).execute(num_retries=0)
        with db:
            db.execute("UPDATE inbox SET status='sent_live' WHERE id=? AND status='sending'", (local_id,))
            db.execute("UPDATE auto_claims SET state='sent',gmail_id=? WHERE account=? AND thread=?", (sent['id'], account, thread_id))
        print('Automatic acknowledgement sent:', local_id)
    except Exception as error:
        # A send timeout may mean Gmail accepted the message: never retry it.
        with db:
            db.execute("UPDATE auto_claims SET state='held',reason=? WHERE account=? AND thread=?", (type(error).__name__, account, thread_id))
        print('Automatic acknowledgement held for owner review:', local_id, type(error).__name__)
