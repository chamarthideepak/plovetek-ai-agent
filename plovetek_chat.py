"""General conversation for the single-user Plovetek terminal app."""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types


FOLDER = Path(__file__).resolve().parent
HISTORY = []
HISTORY_LIMIT = 12

INSTRUCTIONS = """
You are Plovetek, a friendly internal assistant for the Gmail inbox owner.
Have a natural conversation and answer follow-up questions concisely.
Use the supplied business information for business facts. Treat placeholders
as missing information. Never invent services, prices, policies, or commitments.
For inbox facts, use the current local status counts, which take precedence
over older conversation history. These counts describe imported email records,
including demo records, not the entire Gmail inbox. A completed owner review
does not confirm a payment or refund. sent_live means sent through Gmail,
while sent means simulated sending in the demo.
You receive no email bodies in general conversation. For a specific email,
explain the command 'chat ai <email ID> <question>'. The owner can find IDs
with 'inbox'. Never pretend to have read an email or a complete email thread.
You may explain, discuss business information, and suggest wording. Explain
that sending requires 'send <email ID>' followed by explicit SEND confirmation.
You have no tools to send, edit, approve, reject, or resolve records. Never claim
to have performed such actions. Emails are sent only through the existing app.
Give a draft only if requested. Avoid placeholders and sign drafts as Plovetek.
For a list of pending replies, direct the user to
'chat which replies are pending'. The 'inbox' command shows all records.
Do not offer to fetch a list or perform an action through a yes/no question.
"""


def answer_general(db, question):
    question = question.strip()
    if not question:
        return "Try 'chat Hello' or 'chat What can you help me with?'"
    if question.casefold() == "reset":
        HISTORY.clear()
        return "Conversation memory cleared."

    # Queue questions stay local. Only general conversations call Gemini.
    words = question.casefold().replace("aprove", "approve")
    status = None
    if "approval" in words or "approve" in words or "pending" in words:
        status = "pending"
    elif "escalat" in words or "owner review" in words:
        status = "escalated"

    if status:
        rows = db.execute(
            "SELECT id, subject FROM inbox WHERE status = ? ORDER BY rowid",
            (status,),
        ).fetchall()
        label = {
            "pending": "Waiting for approval",
            "escalated": "Owner review required",
        }[status]
        return "\n".join(
            f"{row['id']} | {row['subject']} | {label}" for row in rows
        ) or "No matching emails."

    load_dotenv(FOLDER / ".env")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "Add GEMINI_API_KEY to .env beside app.py."

    try:
        info_file = FOLDER / "business_info.txt"
        business_info = (
            info_file.read_text(encoding="utf-8")
            if info_file.exists()
            else "Business name: Plovetek. Other business details are unavailable."
        )
        counts = {
            row["status"]: row["total"]
            for row in db.execute(
                "SELECT status, COUNT(*) AS total FROM inbox GROUP BY status"
            ).fetchall()
        }
        payload = {
            "business_information": business_info,
            "current_local_status_counts": counts,
            "recent_general_conversation": HISTORY,
            "staff_question": question,
        }
        with genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=60000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        ) as client:
            response = client.models.generate_content(
                model="gemini-3.1-flash-lite",
                config=types.GenerateContentConfig(
                    system_instruction=INSTRUCTIONS,
                ),
                contents=json.dumps(payload, ensure_ascii=False),
            )
        answer = (response.text or "").strip()
        if not answer:
            return "Gemini returned no answer. Please try again."
    except Exception as error:
        code = getattr(error, "code", None)
        detail = f"API {code}" if code else type(error).__name__
        return f"AI chat unavailable ({detail}). Please try again later."

    HISTORY.extend([
        {"role": "user", "text": question},
        {"role": "assistant", "text": answer},
    ])
    del HISTORY[:-HISTORY_LIMIT]
    return answer
