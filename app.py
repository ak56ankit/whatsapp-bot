import os, re, time, logging
from typing import Dict, Any
from flask import Flask, request, abort
from dotenv import load_dotenv

# Twilio response helper (no Twilio client needed for replies)
from twilio.twiml.messaging_response import MessagingResponse

# Optional: OpenAI (LLM replies). If you don't want AI, leave OPENAI_API_KEY unset.
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

load_dotenv()

APP_NAME = os.getenv("APP_NAME", "ZP WhatsApp Bot")
ADMIN_PASSCODE = os.getenv("ADMIN_PASSCODE", "1234")  # simple admin gate for quick ops

# Optional AI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")  # cheap+fast; change if you wish

# Basic in-memory session store (replace with Redis/Postgres for production)
SESSIONS: Dict[str, Dict[str, Any]] = {}

def get_ai_client():
    if OPENAI_API_KEY and OpenAI is not None:
        return OpenAI(api_key=OPENAI_API_KEY)
    return None

AI = get_ai_client()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)


# --- Utilities ----------------------------------------------------------------
def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()

def reply_text(body: str) -> str:
    """Return TwiML XML as string with a single WhatsApp message reply."""
    resp = MessagingResponse()
    resp.message(body)
    return str(resp)

def session_for(wa_from: str) -> Dict[str, Any]:
    if wa_from not in SESSIONS:
        SESSIONS[wa_from] = {
            "created_at": time.time(),
            "context": {},
            "last_intent": None,
            "history": []
        }
    return SESSIONS[wa_from]

def small_menu() -> str:
    return (
        f"*{APP_NAME}*\n"
        "Type a number or keyword:\n"
        "1) Schemes & Eligibility\n"
        "2) Lodge a grievance\n"
        "3) Check application status\n"
        "4) Contact / Office hours\n"
        "5) Help (commands)\n"
        "_Tip: send ‘menu’ anytime._"
    )

def help_text() -> str:
    return (
        "Commands:\n"
        "• menu — main options\n"
        "• schemes — PMAY, JJM, SHG, etc.\n"
        "• status <ID> — check an application (demo)\n"
        "• grievance — file a grievance (demo)\n"
        "• stop — forget my session\n"
        "• admin <pass> ping|stats|reset — admin ops\n"
        "For general questions, just ask in plain English/Marathi/Hindi."
    )

def schemes_text() -> str:
    return (
        "*Schemes (demo)*\n"
        "• PMAY-G: Rural housing support\n"
        "• JJM: Functional tap connection\n"
        "• SHG: Livelihood & credit linkages\n"
        "• MGNREGS: Wage employment\n"
        "_Reply with the scheme name for basics. For eligibility, send: ‘eligibility <scheme> <your details>’_"
    )

def contact_text() -> str:
    return (
        "*Contact (demo)*\n"
        "• Zilla Parishad Helpline: 1800-000-000\n"
        "• Office hours: Mon–Fri 10:30–17:30\n"
        "• Email: help@zp.example.in\n"
        "_This is a demo. Replace with your actual contacts._"
    )

def status_lookup(app_id: str) -> str:
    # Replace with real DB/API lookup
    fake = {
        "PMAY123": ("Under review", "Verification scheduled within 7 days"),
        "JJM456": ("Approved", "Connection expected in 30 days"),
        "SHG789": ("Pending docs", "Please submit bank passbook copy"),
    }
    st = fake.get(app_id.upper())
    if not st:
        return f"No record found for *{app_id}*. Check the ID and try again."
    return f"*Status for {app_id}:* {st[0]}\nNote: {st[1]}"

def admin_ops(parts):
    # admin <pass> <cmd>
    if len(parts) < 3:
        return "Usage: admin <passcode> <ping|stats|reset>"
    if parts[1] != ADMIN_PASSCODE:
        return "Admin: invalid passcode."
    cmd = parts[2]
    if cmd == "ping":
        return "pong ✅"
    if cmd == "stats":
        return f"Sessions: {len(SESSIONS)}"
    if cmd == "reset":
        SESSIONS.clear()
        return "All sessions cleared."
    return "Unknown admin command."

def trivial_intents(text: str, sess: Dict[str, Any]) -> str | None:
    t = normalize(text)

    # direct menu numbers
    if t in {"1", "schemes", "scheme", "yojana"}:
        sess["last_intent"] = "schemes"
        return schemes_text()
    if t in {"2", "grievance", "complaint"}:
        sess["last_intent"] = "grievance"
        return (
            "*Grievance (demo)*\n"
            "Reply in this format:\n"
            "`grievance <name> <village> <issue>`\n"
            "Example: `grievance Priya Pal ‘Khadakde’ water not reaching last mile`\n"
            "You’ll get a ticket ID back."
        )
    if t in {"3", "status"}:
        sess["last_intent"] = "status"
        return "Send: `status <APPLICATION_ID>`\nExample: `status PMAY123`"
    if t in {"4", "contact"}:
        sess["last_intent"] = "contact"
        return contact_text()
    if t in {"5", "help"}:
        sess["last_intent"] = "help"
        return help_text()
    if t in {"menu", "start", "hi", "hello", "namaste", "नमस्ते", "नमस्कार"}:
        sess["last_intent"] = "menu"
        return small_menu()

    # structured patterns
    if t.startswith("status "):
        app_id = t.split(" ", 1)[1].strip()
        sess["last_intent"] = "status_lookup"
        return status_lookup(app_id)

    if t.startswith("grievance "):
        payload = text.strip()[len("grievance "):].strip()
        if len(payload) < 4:
            return "Please include some details: `grievance <name> <village> <issue>`"
        # Fake ticket
        ticket = f"GRV{int(time.time())%100000:05d}"
        sess["last_intent"] = "grievance_ticket"
        sess["context"]["last_ticket"] = ticket
        return (
            f"Thanks. Ticket *{ticket}* created.\n"
            "You’ll receive an update after initial triage (ETA 48h, demo).\n"
            "To add details, just reply in this chat."
        )

    if t.startswith("admin "):
        return admin_ops(t.split())

    if t == "stop":
        SESSIONS.pop(sess.get("id",""), None)
        return "Session cleared. Send ‘menu’ to start again."

    return None


def ai_answer(user_text: str, sess: Dict[str, Any]) -> str:
    """
    If OPENAI_API_KEY is set, ask an LLM for a helpful, short answer.
    Keep it safe and concise for WhatsApp.
    """
    if not AI:
        return ""
    try:
        # System prompt steers style; keep replies compact for WhatsApp
        system = (
            "You are a helpful government service assistant. "
            "Answer concisely (max ~3 sentences). Use simple language. "
            "If you mention timelines or policies, add a gentle disclaimer like "
            "“process may vary by district—verify locally.”"
        )
        msg = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text}
        ]
        r = AI.chat.completions.create(model=AI_MODEL, messages=msg, temperature=0.2)
        return (r.choices[0].message.content or "").strip()
    except Exception as e:
        logging.exception("AI error: %s", e)
        return ""


# --- Routes -------------------------------------------------------------------
@app.route("/", methods=["GET"])
def health():
    return f"{APP_NAME} is up"

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    # Twilio will send form-encoded payload
    wa_from = request.values.get("From", "")
    body = request.values.get("Body", "")
    if not wa_from:
        abort(400)

    sess = session_for(wa_from)
    sess["id"] = wa_from
    sess["history"].append(("user", body))

    # 1) Try deterministic intents first
    msg = trivial_intents(body, sess)
    if not msg:
        # 2) If not matched, try AI fallback if available
        msg = ai_answer(body, sess)
    if not msg:
        # 3) Final fallback
        msg = (
            "I didn’t catch that. Here’s the menu:\n\n" + small_menu()
        )

    sess["history"].append(("bot", msg))
    return reply_text(msg)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
