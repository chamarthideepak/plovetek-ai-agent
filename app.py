"""Plovetek Phase 1 terminal app for Gmail review and conversation."""

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

SAMPLES = [
    ("newsletter-1", "news@example.com", "Weekly newsletter", "This week's updates."),
    ("enquiry-1", "customer@example.com", "Can I get a quotation?", "Please send your pricing."),
    ("refund-1", "buyer@example.com", "Refund request", "Please refund my last payment."),
    ("task-1", "team@example.com", "Internal follow-up", "Please assign an internal task."),
    ("unknown-1", "sender@example.com", "Hello", "Following up on yesterday."),
]

LABELS = {
    "ignored": "No reply needed",
    "task": "Internal follow-up",
    "pending": "Waiting for approval",
    "escalated": "Owner review required",
    "rejected": "Draft rejected",
    "sent": "Sent in demo",
    "sending": "Send needs verification",
    "sent_live": "Sent through Gmail",
    "resolved": "Owner review completed",
}

HELP = (
    "Type a question naturally, or use chat <question>.\n"
    "Commands: seed, receive, inbox, summarize, chat, edit, send, approve, "
    "reject, resolve, help, quit\n"
    "send <email ID> opens live Gmail review and asks for SEND confirmation.\n"
    "approve <email ID> records a demo send.\n"
    "Example: summarize gmail-MESSAGE_ID"
)


def open_db(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE IF NOT EXISTS inbox (
            id TEXT PRIMARY KEY, sender TEXT NOT NULL, subject TEXT NOT NULL,
            body TEXT NOT NULL, status TEXT NOT NULL, draft TEXT
        )
    """)
    return db


def receive(db, message):
    message_id, sender, subject, body = message
    content = f"{subject} {body}".casefold()
    draft = None
    normalized_subject = " ".join(subject.casefold().split()).replace(
        "news letter", "newsletter"
    )

    if any(term in content for term in (
        "refund", "payment", "contract", "security alert",
        "suspicious sign-in", "password reset",
    )):
        status = "escalated"
    elif normalized_subject.startswith((
        "weekly newsletter", "delivery receipt", "automated notification",
    )):
        status = "ignored"
    elif any(term in content for term in (
        "quotation request",
        "quatation request",
        "please send your pricing",
        "please send me your pricing",
        "please provide a quotation",
        "request a quote",
        "complaint",
    )):
        status = "pending"
        draft = "Hello, thank you for reaching out. We’ll review your request and follow up."
    elif "internal follow-up" in content:
        status = "task"
    elif any(term in content for term in ("newsletter", "automated alert", "delivery receipt")):
        status = "ignored"
    else:
        status = "escalated"
    if status == "pending" and message_id.startswith("gmail-"):
        existing = db.execute(
            "SELECT 1 FROM inbox WHERE id = ?", (message_id,),
        ).fetchone()
        if existing:
            return

        try:
            from ai_drafting import prepare_draft

            draft = prepare_draft(subject, body)
        except Exception as error:
            print(
                f"AI drafting failed ({type(error).__name__}). "
                "Email queued for owner review."
            )
            status = "escalated"
            draft = None
    with db:
        db.execute(
            "INSERT INTO inbox VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
            (message_id, sender, subject, body, status, draft),
        )


def show(db, status=None):
    rows = db.execute(
        "SELECT id, subject, status FROM inbox WHERE ? IS NULL OR status = ? ORDER BY rowid",
        (status, status),
    ).fetchall()
    return "\n".join(f"{r['id']} | {r['subject']} | {LABELS[r['status']]}" for r in rows) or "No matching emails."


def ask(db, question):
    if question.strip().casefold().startswith("ai "):
        parts = question.strip().split(maxsplit=2)
        if len(parts) != 3:
            return "Usage: chat ai <email ID> <question>"

        _, message_id, ai_question = parts
        row = db.execute(
            "SELECT * FROM inbox WHERE id = ?", (message_id,),
        ).fetchone()
        if row is None:
            return "Email ID not found."
        email_data = dict(row)
        email_data["status"] = LABELS.get(row["status"], row["status"])

        try:
            from ai_chat import answer_email

            return answer_email(ai_question, email_data)
        except Exception as error:
            return (
                f"AI chat failed ({type(error).__name__}). "
                "Check your connection and Gemini quota, then retry."
            )

    words = question.strip().casefold()
    if words == "summarize":
        return "Usage: summarize <email ID>"
    if words.startswith("summarize "):
        message_id = question.strip().split(maxsplit=1)[1].strip()
        message_id = re.sub(
            r"^(?:the\s+)?email\s+", "", message_id, flags=re.IGNORECASE,
        ).strip()
        row = db.execute("SELECT * FROM inbox WHERE id = ?", (message_id,)).fetchone()
        if row is None and re.fullmatch(r"[0-9a-fA-F]+", message_id):
            row = db.execute(
                "SELECT * FROM inbox WHERE id = ?",
                (f"gmail-{message_id.lower()}",),
            ).fetchone()
        if row is None:
            return "Email ID not found."
        excerpt = " ".join(row["body"].split())
        if len(excerpt) > 220:
            excerpt = excerpt[:217] + "..."
        lines = [
            f"Subject: {row['subject']}", f"From: {row['sender']}",
            f"Message excerpt: {excerpt}", f"Status: {LABELS[row['status']]}",
        ]
        if row["status"] == "escalated":
            lines.append("Next step: Gmail owner reviews and decides how to proceed.")
        if row["draft"]:
            lines.append(f"Reply: {row['draft']}")
        return "\n".join(lines)
    for term, status in (("pending", "pending"), ("escalat", "escalated"), ("task", "task")):
        if term in words:
            return show(db, status)
    try:
        from plovetek_chat import answer_general

        return answer_general(db, question)
    except Exception as error:
        return (
            f"General chat unavailable ({type(error).__name__}). "
            "Check plovetek_chat.py and your Gemini setup. "
            "Use inbox or summarize <email ID> to review saved emails."
        )


def review(db, message_id, action, draft=None):
    with db:
        if action == "edit":
            result = db.execute(
                "UPDATE inbox SET draft = ? WHERE id = ? AND status = 'pending'",
                (draft, message_id),
            )
        else:
            status = {"approve": "sent", "reject": "rejected"}[action]
            result = db.execute(
                "UPDATE inbox SET status = ? WHERE id = ? AND status = 'pending'",
                (status, message_id),
            )
    if result.rowcount != 1:
        return "That email has no draft waiting for approval."
    return {
        "edit": "Draft updated. It still needs approval.",
        "approve": "Approved and marked sent in the demo. No real email was sent.",
        "reject": "Draft rejected.",
    }[action]


def main():
    print("Plovetek Phase 1 | Gmail review and AI chat | Local owner session")
    print(HELP)
    with closing(open_db(Path(__file__).with_name("demo.sqlite3"))) as db:
        while True:
            try:
                line = input("\nplovetek> ").strip()
                if not line:
                    continue
                command, _, argument = line.partition(" ")
                command = command.casefold()
                if command == "quit":
                    return
                if command == "help":
                    print(HELP)
                elif command == "seed":
                    for message in SAMPLES:
                        receive(db, message)
                    print(show(db))
                elif command == "receive":
                    message = json.loads(argument)
                    fields = ("id", "sender", "subject", "body")
                    if not isinstance(message, dict) or not all(
                        isinstance(message.get(field), str) and message[field].strip()
                        for field in fields
                    ):
                        raise ValueError("Provide nonempty id, sender, subject and body strings.")
                    receive(db, tuple(message[field] for field in fields))
                    print(show(db))
                elif command == "send":
                    if not argument.strip():
                        print("Usage: send <email ID>")
                        continue
                    try:
                        from gmail_send_test import main as send_gmail

                        send_gmail(argument.strip())
                    except SystemExit as error:
                        print(error)
                elif command == "resolve":
                    message_id = argument.strip()
                    if not message_id:
                        print("Usage: resolve <email ID>")
                        continue
                    with db:
                        result = db.execute(
                            "UPDATE inbox SET status = 'resolved' "
                            "WHERE id = ? AND status = 'escalated'",
                            (message_id,),
                        )
                    if result.rowcount == 1:
                        print("Owner review completed.")
                    else:
                        print("That email is not awaiting owner review.")
                elif command == "inbox":
                    print(show(db))
                elif command == "chat":
                    print(ask(db, argument))
                elif command in ("approve", "reject"):
                    if not argument.strip():
                        print(f"Usage: {command} <email ID>")
                        continue
                    print(review(db, argument.strip(), command))
                elif command == "edit":
                    message_id, _, draft = argument.partition(" ")
                    if not draft.strip():
                        raise ValueError("Use edit <email ID> <reply text>.")
                    print(review(db, message_id, command, draft.strip()))
                else:
                    print(ask(db, line))
            except ValueError as error:
                print(f"Input error: {error}")
            except (EOFError, KeyboardInterrupt):
                print()
                return


if __name__ == "__main__":
    main()
