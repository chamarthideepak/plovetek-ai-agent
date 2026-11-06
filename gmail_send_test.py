import base64
import sys
from contextlib import closing
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app import open_db


FOLDER = Path(__file__).resolve().parent
TOKEN_FILE = FOLDER / "gmail_send_token.json"
CREDENTIALS_FILE = FOLDER / "credentials.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


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
            raise SystemExit("Place credentials.json beside gmail_send_test.py.")

        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_FILE), SCOPES
        )
        credentials = flow.run_local_server(port=0)

    temporary_token = TOKEN_FILE.with_suffix(".tmp")
    temporary_token.write_text(credentials.to_json(), encoding="utf-8")
    temporary_token.replace(TOKEN_FILE)
    return credentials


def main(local_id=None):
    if local_id is None:
        if len(sys.argv) != 2:
            raise SystemExit(
                "Usage: python gmail_send_test.py gmail-MESSAGE_ID"
            )
        local_id = sys.argv[1]

    local_id = local_id.strip()
    if not local_id.startswith("gmail-") or local_id == "gmail-":
        raise SystemExit("Use the gmail- ID of a pending draft.")

    with closing(open_db(FOLDER / "demo.sqlite3")) as db:
        row = db.execute(
            """
            SELECT draft FROM inbox
            WHERE id = ? AND status = 'pending'
            """,
            (local_id,),
        ).fetchone()

        if row is None or not row["draft"]:
            raise SystemExit("This email has no draft waiting for approval.")

        draft_text = row["draft"]

        credentials = get_credentials()
        users = build("gmail", "v1", credentials=credentials).users()
        account = users.getProfile(userId="me").execute()["emailAddress"]
        messages = users.messages()

        original = messages.get(
            userId="me",
            id=local_id.removeprefix("gmail-"),
            format="metadata",
        ).execute()

        headers = {
            header["name"].lower(): header["value"]
            for header in original["payload"].get("headers", [])
        }

        reply_address = headers.get("reply-to") or headers.get("from", "")
        recipients = getaddresses([reply_address])
        original_message_id = headers.get("message-id")

        if (
            len(recipients) != 1
            or not recipients[0][1]
            or not original_message_id
        ):
            raise SystemExit(
                "Could not verify a single reply address and Message-ID."
            )

        recipient = recipients[0][1]
        subject = headers.get("subject", "")

        reply = EmailMessage()
        reply["From"] = account
        reply["To"] = recipient
        reply["Subject"] = subject
        reply["In-Reply-To"] = original_message_id
        reply["References"] = (
            f"{headers.get('references', '')} {original_message_id}"
        ).strip()
        reply.set_content(draft_text)

        send_body = {
            "threadId": original["threadId"],
            "raw": base64.urlsafe_b64encode(
                reply.as_bytes()
            ).decode("ascii"),
        }

        print(f"\nFrom: {account}")
        print(f"To: {recipient}")
        print(f"Subject: {subject}")
        print(f"\n{draft_text}\n")

        confirmation = input(
            "Type SEND to approve and send this real email: "
        )
        if confirmation.strip() != "SEND":
            print("Cancelled. The draft is still waiting for approval.")
            return

        # Claim the exact draft displayed for approval.
        with db:
            claimed = db.execute(
                """
                UPDATE inbox SET status = 'sending'
                WHERE id = ? AND status = 'pending' AND draft = ?
                """,
                (local_id, draft_text),
            )

        if claimed.rowcount != 1:
            raise SystemExit(
                "The draft changed or was already processed. Review it again."
            )

        try:
            sent = messages.send(
                userId="me", body=send_body
            ).execute(num_retries=0)
        except Exception as error:
            raise SystemExit(
                f"Gmail send could not be confirmed "
                f"({type(error).__name__}). "
                "Check Gmail Sent. The draft is held for review "
                "to prevent an automatic duplicate send."
            ) from None

        try:
            with db:
                db.execute(
                    """
                    UPDATE inbox SET status = 'sent_live'
                    WHERE id = ? AND status = 'sending'
                    """,
                    (local_id,),
                )
        except Exception:
            raise SystemExit(
                f"Gmail sent the reply. Message ID: {sent['id']}. "
                "The local status could not be updated. "
                "Check Gmail Sent before taking further action."
            ) from None

        print(f"Reply sent through Gmail. Gmail message ID: {sent['id']}")


if __name__ == "__main__":
    main()