"""
Gmail Inbox Agent
Two-stage pipeline: Haiku triage → Sonnet draft reply → Gmail draft saved
"""

import os
import base64
import re
import glob
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import anthropic
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ─── CONFIG ──────────────────────────────────────────────────────────────────

AI_BRAIN_FOLDER  = os.path.expanduser("~/Documents/AI Brain")
CREDENTIALS_FILE = "credentials.json"                  # from Google Cloud Console
TOKEN_FILE       = "token.json"                        # auto-created on first run
MAX_EMAILS       = 20                                  # max unread emails to process per run
PROCESSED_LABEL  = "AI-Processed"                      # Gmail label to prevent re-processing

MY_NAME    = "Bruce Pemberton-Billing"
MY_ROLE    = "COO"
MY_EMAIL   = "bruce@carpediemtours.com"
COMPANY    = "Carpe Diem Tours"
COMPANY_CONTEXT = (
    "Carpe Diem Tours runs guided tours across Rome, Florence, Barcelona, Lisbon, "
    "Madrid, Budapest, and London. Our email mix covers operations, partner relations, "
    "legal matters, and HR. Tone should be direct and professional."
)

# ─── INTERNAL CONSTANTS ───────────────────────────────────────────────────────

SCOPES       = ["https://www.googleapis.com/auth/gmail.modify"]
TRIAGE_MODEL = "claude-haiku-4-5-20251001"
DRAFT_MODEL  = "claude-sonnet-4-6"

TRIAGE_TOOL = {
    "name": "triage_email",
    "description": "Classify an incoming email and extract key metadata for routing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "classification": {
                "type": "string",
                "enum": ["needs-reply", "fyi-only", "no-action-needed"],
            },
            "urgency": {
                "type": "string",
                "enum": ["high", "medium", "low"],
            },
            "category": {
                "type": "string",
                "enum": ["operational", "partner", "legal", "hr", "other"],
            },
            "summary": {"type": "string"},
            "context_keywords": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["classification", "urgency", "category", "summary", "context_keywords"],
    },
}

# ─── GMAIL AUTH ───────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ─── EMAIL FETCHING ───────────────────────────────────────────────────────────

def get_unread_emails(service, max_results=MAX_EMAILS):
    query = f'-label:{PROCESSED_LABEL} (in:inbox OR label:"Follow Up")'
    result = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    return result.get("messages", [])


def extract_body(payload):
    """Recursively extract plain-text body from a MIME payload."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            return re.sub(r"<[^>]+>", " ", html)

    for part in payload.get("parts", []):
        text = extract_body(part)
        if text:
            return text

    return ""


def parse_email(message):
    headers = {h["name"]: h["value"] for h in message["payload"].get("headers", [])}
    body = extract_body(message["payload"])
    return {
        "id":               message["id"],
        "thread_id":        message["threadId"],
        "message_id_header": headers.get("Message-ID", ""),
        "from":             headers.get("From", ""),
        "to":               headers.get("To", ""),
        "subject":          headers.get("Subject", "(no subject)"),
        "date":             headers.get("Date", ""),
        "body":             body[:3000],
    }


# ─── SENT EMAIL SAMPLES ──────────────────────────────────────────────────────

def get_sent_samples(service, max_results=10):
    """Return a block of recent sent email bodies to use as tone-of-voice examples."""
    result = service.users().messages().list(
        userId="me", q="in:sent", maxResults=max_results
    ).execute()
    refs = result.get("messages", [])

    samples = []
    for ref in refs:
        try:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="full"
            ).execute()
            body = extract_body(msg["payload"])[:600].strip()
            if body:
                samples.append(body)
        except Exception:
            continue

    if not samples:
        return ""

    joined = "\n\n---\n\n".join(samples)
    return f"--- EXAMPLES OF MY PREVIOUS EMAIL REPLIES (for tone and style reference) ---\n\n{joined}\n\n--- END EXAMPLES ---"


# ─── TRIAGE ───────────────────────────────────────────────────────────────────

def triage_email(client, email):
    prompt = (
        f"Triage this email.\n\n"
        f"From: {email['from']}\n"
        f"Subject: {email['subject']}\n"
        f"Date: {email['date']}\n\n"
        f"{email['body']}"
    )
    response = client.messages.create(
        model=TRIAGE_MODEL,
        max_tokens=512,
        tools=[TRIAGE_TOOL],
        tool_choice={"type": "tool", "name": "triage_email"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "triage_email":
            return block.input
    return None


# ─── CONTEXT FILE LOADING ─────────────────────────────────────────────────────

def load_context_files(keywords, category):
    """Score .md files in AI_BRAIN_FOLDER by keyword relevance; return top 3 (≤4000 chars total)."""
    if not os.path.isdir(AI_BRAIN_FOLDER):
        return ""

    pattern = os.path.join(AI_BRAIN_FOLDER, "**", "*.md")
    candidates = glob.glob(pattern, recursive=True)
    if not category:
        category = ""

    scored = []
    search_terms = [k.lower() for k in keywords] + [category.lower()]

    for path in candidates:
        score = 0
        fname = os.path.basename(path).lower()
        for term in search_terms:
            if term and term in fname:
                score += 2
        try:
            content = open(path, encoding="utf-8").read()
        except OSError:
            continue
        lower = content.lower()
        for term in search_terms:
            if term:
                score += lower.count(term)
        if score > 0:
            scored.append((score, path, content))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:3]

    chunks = []
    total = 0
    for _, path, content in top:
        snippet = content[: max(0, 4000 - total)]
        if not snippet:
            break
        chunks.append(f"### {os.path.basename(path)}\n{snippet}")
        total += len(snippet)

    return "\n\n".join(chunks)


# ─── DRAFT GENERATION ─────────────────────────────────────────────────────────

def draft_reply(client, email, triage, context_docs, sent_samples=""):
    system = (
        f"You are {MY_NAME}, {MY_ROLE} of {COMPANY}. "
        f"{COMPANY_CONTEXT} "
        f"Write a reply as {MY_NAME}. "
        f"Match the tone, vocabulary, and sentence length shown in the example replies below — "
        f"do not be more formal or more casual than those examples. "
        f"Do not add a sign-off — the user will review before sending."
    )

    samples_section = f"\n\n{sent_samples}" if sent_samples else ""
    context_section = f"\n\n--- CONTEXT FROM AI BRAIN ---\n{context_docs}\n--- END CONTEXT ---" if context_docs else ""

    user_prompt = (
        f"Please draft a reply to the following email.{samples_section}{context_section}\n\n"
        f"From: {email['from']}\n"
        f"Subject: {email['subject']}\n"
        f"Date: {email['date']}\n\n"
        f"{email['body']}\n\n"
        f"Triage summary: {triage.get('summary', '')}\n"
        f"Urgency: {triage.get('urgency', '')}\n"
        f"Category: {triage.get('category', '')}"
    )

    response = client.messages.create(
        model=DRAFT_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return response.content[0].text


# ─── GMAIL DRAFT CREATION ─────────────────────────────────────────────────────

def create_gmail_draft(service, email, reply_text):
    msg = MIMEMultipart()
    msg["To"]         = email["from"]
    msg["From"]       = MY_EMAIL
    msg["Subject"]    = f"Re: {email['subject']}"
    msg["In-Reply-To"] = email["message_id_header"]
    msg["References"]  = email["message_id_header"]
    msg.attach(MIMEText(reply_text, "plain"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft_body = {
        "message": {
            "raw": raw,
            "threadId": email["thread_id"],
        }
    }
    draft = service.users().drafts().create(userId="me", body=draft_body).execute()
    return draft["id"]


# ─── LABEL MANAGEMENT ────────────────────────────────────────────────────────

def ensure_label(service, label_name):
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"] == label_name:
            return label["id"]

    new_label = service.users().labels().create(
        userId="me",
        body={"name": label_name, "messageListVisibility": "hide", "labelListVisibility": "labelHide"},
    ).execute()
    return new_label["id"]


def mark_processed(service, message_id, label_id):
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": [label_id]},
    ).execute()


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set.")

    client  = anthropic.Anthropic(api_key=api_key)
    service = get_gmail_service()

    label_id = ensure_label(service, PROCESSED_LABEL)

    print("Loading sent email samples for tone reference...")
    sent_samples = get_sent_samples(service)

    messages = get_unread_emails(service)
    if not messages:
        print("No unread emails to process.")
        return

    print(f"Found {len(messages)} unread email(s) to process.\n")

    counts = {"needs-reply": 0, "fyi-only": 0, "no-action-needed": 0, "errors": 0}

    for msg_ref in messages:
        try:
            full_msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ).execute()
            email = parse_email(full_msg)

            print(f"[{email['subject'][:60]}]  from: {email['from'][:40]}")

            triage = triage_email(client, email)
            if not triage:
                print("  → triage failed, skipping\n")
                counts["errors"] += 1
                continue

            classification = triage.get("classification", "no-action-needed")
            print(f"  → {classification} | urgency={triage.get('urgency')} | category={triage.get('category')}")
            counts[classification] = counts.get(classification, 0) + 1

            if classification == "needs-reply":
                context_docs = load_context_files(
                    triage.get("context_keywords", []),
                    triage.get("category", ""),
                )
                reply_text = draft_reply(client, email, triage, context_docs, sent_samples)
                draft_id   = create_gmail_draft(service, email, reply_text)
                print(f"  → draft saved (id={draft_id})")

            mark_processed(service, email["id"], label_id)
            print()

        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR: {exc}\n")
            counts["errors"] += 1

    print("─" * 50)
    print(f"Processed {len(messages)} email(s):")
    print(f"  needs-reply      : {counts['needs-reply']}")
    print(f"  fyi-only         : {counts['fyi-only']}")
    print(f"  no-action-needed : {counts['no-action-needed']}")
    if counts["errors"]:
        print(f"  errors           : {counts['errors']}")


if __name__ == "__main__":
    main()
