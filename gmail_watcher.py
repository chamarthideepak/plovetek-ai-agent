import base64
import argparse
import time
from contextlib import closing
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app import ask, open_db, receive


FOLDER = Path(__file__).resolve().parent
CREDENTIALS_FILE = FOLDER / "credentials.json"
TOKEN_FILE = FOLDER / "gmail_readonly_token.json"
DB_PATH = FOLDER / "demo.sqlite3"

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CHECK_INTERVAL = 15


def get_credentials():
    credentials = None

    if TOKEN_FILE.exists():
        try:
            credentials = Credentials.from_authorized_user_file(
                str(TOKEN_FILE), SCOPES
            )
        except ValueError:
            print("Saved authorization could not be read. Sign in again.")

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError:
            print("Saved authorization expired or was revoked. Sign in again.")
            credentials = None

    if credentials is None or not credentials.valid:
        if not CREDENTIALS_FILE.exists():
            raise SystemExit("Place credentials.json beside gmail_watcher.py.")

        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_FILE), SCOPES
        )
        credentials = flow.run_local_server(port=0)

    temporary_token = TOKEN_FILE.with_suffix(".tmp")
    temporary_token.write_text(credentials.to_json(), encoding="utf-8")
    temporary_token.replace(TOKEN_FILE)
    return credentials


def read_email_body(payload):
    if payload.get("mimeType") == "text/plain":
        encoded = payload.get("body", {}).get("data")
        if encoded:
            padded = encoded + "=" * (-len(encoded) % 4)
            decoded = base64.urlsafe_b64decode(padded)

            content_type = next(
                (
                    header["value"]
                    for header in payload.get("headers", [])
                    if header["name"].lower() == "content-type"
                ),
                "text/plain",
            )

            from email.message import Message

            mime_header = Message()
            mime_header["Content-Type"] = content_type
            charset = mime_header.get_content_charset() or "utf-8"

            try:
                return decoded.decode(charset, errors="replace")
            except LookupError:
                return decoded.decode("utf-8", errors="replace")

    texts = []
    for part in payload.get("parts", []):
        if part.get("filename"):
            continue
        text = read_email_body(part)
        if text:
            texts.append(text)

    return "\n".join(texts)


def get_watch_start(account):
    with closing(open_db(DB_PATH)) as db:
        with db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS gmail_watch_start (
                    account TEXT PRIMARY KEY,
                    started_at INTEGER NOT NULL
                )
            """)
            db.execute(
                """
                INSERT INTO gmail_watch_start (account, started_at)
                VALUES (?, ?)
                ON CONFLICT(account) DO NOTHING
                """,
                (account, int(time.time())),
            )

        row = db.execute(
            "SELECT started_at FROM gmail_watch_start WHERE account = ?",
            (account,),
        ).fetchone()

    return row["started_at"]


def import_messages(messages, started_at, users=None, account=None, auto_since=None):
    page_token = None

    while True:
        result = messages.list(
            userId="me",
            labelIds=["INBOX"],
            q=f"after:{started_at}",
            pageToken=page_token,
        ).execute()

        for item in result.get("messages", []):
            message_id = f"gmail-{item['id']}"

            with closing(open_db(DB_PATH)) as db:
                existing = db.execute(
                    "SELECT 1 FROM inbox WHERE id = ?",
                    (message_id,),
                ).fetchone()

            if existing:
                continue

            message = messages.get(
                userId="me",
                id=item["id"],
                format="full",
            ).execute()

            payload = message.get("payload", {})
            headers = {
                header["name"].lower(): header["value"]
                for header in payload.get("headers", [])
            }

            body = read_email_body(payload) or message.get("snippet", "")

            with closing(open_db(DB_PATH)) as db:
                receive(
                    db,
                    (
                        message_id,
                        headers.get("from", "(unknown sender)"),
                        headers.get("subject", "(no subject)"),
                        body,
                    ),
                )

                if auto_since is not None:
                    from auto_reply import process
                    try:
                        process(db, users, account, message, read_email_body(payload), auto_since)
                    except Exception as error:
                        print('Auto policy check failed; held for review:', type(error).__name__)

                print(f"\nImported {message_id}")
                print(ask(db, f"Summarize {message_id}"))

        page_token = result.get("nextPageToken")
        if not page_token:
            return


def main():
    parser = argparse.ArgumentParser(description='Watch incoming Gmail messages')
    parser.add_argument('--auto-send', action='store_true', help='Enable real new-enquiry acknowledgements')
    args = parser.parse_args()
    if args.auto_send:
        from gmail_send_test import get_credentials as get_send_credentials
        credentials = get_send_credentials()
    else:
        credentials = get_credentials()
    gmail = build("gmail", "v1", credentials=credentials)
    users = gmail.users()

    account = users.getProfile(userId="me").execute()["emailAddress"]
    # This local installation is bound to one Gmail account to protect inbox IDs.
    with closing(open_db(DB_PATH)) as db:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS mailbox_binding (id INTEGER PRIMARY KEY CHECK(id=1), account TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO mailbox_binding VALUES (1,?)', (account.lower(),))
        bound = db.execute('SELECT account FROM mailbox_binding WHERE id=1').fetchone()[0]
        if bound != account.lower():
            raise SystemExit('This database belongs to another Gmail account. Use a separate project folder.')
    started_at = get_watch_start(account)
    auto_since = None
    if args.auto_send:
        from auto_reply import activation
        with closing(open_db(DB_PATH)) as db:
            auto_since = activation(db, account)
        print('AUTO SEND ON: approved new-enquiry acknowledgements only.')
    else:
        print('AUTO SEND OFF: review-only mode.')
    messages = users.messages()

    print(f"Connected Gmail: {account}")
    print(f"Watching Gmail every {CHECK_INTERVAL} seconds.")
    print("Press Ctrl+C to stop.")

    while True:
        try:
            import_messages(messages, started_at, users, account, auto_since)
        except Exception as error:
            print('Watcher check failed; retrying next poll:', type(error).__name__)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nGmail watcher stopped.")