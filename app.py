import os, re, time, logging
from typing import Dict, Any, Optional, Tuple
from flask import Flask, request, abort
from dotenv import load_dotenv

# Twilio reply helper (TwiML)
from twilio.twiml.messaging_response import MessagingResponse

# We use the OpenAI client for BOTH OpenAI and Groq (Groq exposes an OpenAI-compatible API).
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # we'll handle gracefully

load_dotenv()

# ---- Config from environment -------------------------------------------------
APP_NAME = os.getenv("APP_NAME", "ZP WhatsApp Bot")

# Simple admin op passcode
ADMIN_PASSCODE = os.getenv("ADMIN_PASSCODE", "1234")

# LLM provider selection: "groq", "openai", or leave blank to disable AI fallback
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "").strip().lower()

# Keys (set only what you use)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

# Optional base URLs (rarely change; defaults are fine)
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip() or None
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "").strip() or "https://api.groq.com/openai/v1"

# Optional explicit model override (else we pick a sensible default per provider)
AI_MODEL_OVERRIDE = os.getenv("AI_MODEL", "").strip()

# ------------------------------------------------------------------------------
# In-memory sessions (swap for Redis/DB in production)
SESSIONS: Dict[str, Dict[str, Any]] = {}

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)


# ---- LLM wiring --------------------------------------------------------------
def build_ai_client() -> Tuple[Optional[str], Optional[Any], str]:
    """
    Returns: (provider, client, model_name)
      - provider: "groq" | "openai" | None
      - client:   OpenAI client object (or None)
      - model:    resolved model string for the provider
    """
    if OpenAI is None:
        return None, None, ""

    # If user has chosen Groq (recommended for you right now)
    if LLM_PROVIDER == "groq" and GROQ_API_KEY:
        client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL)
        model = AI_MODEL_OVERRIDE or os.getenv("GROQ_MODEL", "llama-3.1-70b-versatile")
        return "groq", client, model

    # If user has chosen OpenAI
    if LLM_PROVIDER == "openai" and OPENAI_API_KEY:
        client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
        model = AI_MODEL_OVERRIDE or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        return "openai", client, model

    # If no explicit provider, auto-select by available key (Groq preferred if present)
    if GROQ_API_KEY:
        client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL)
        model = AI_MODEL_OVERRIDE or os.getenv("GROQ_MODEL", "llama-3.1-70b-versatile")
        return "groq", client, model
    if OPENAI_API_KEY:
        client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
        model = AI_MODEL_OVERRIDE or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        return "openai", client, model

    return None, None, ""


AI_PROVIDER, AI_CLIENT, AI_MODEL = build_ai_client()


# ---- Helpers -----------------------------------------------------------------
def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()

def reply_text(body: str) -> str:
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
        "For general questions, reply in English/Marathi/Hindi."
    )

def schemes_text() -> str:
    return (
        "*Schemes (demo)*\n"
        "• PMAY-G: Rural housing support\n"
        "• JJM: Functional tap connection\n"
        "• SHG: Livelihood & credit linkages\n"
        "• MGNREGS: Wage employment\n"
        "_Reply with ‘eligibility <scheme> <your details>’ for a quick check (demo)._"
    )

def contact_text() -> str:
    return (
        "*Contact (demo)*\n"
        "• Zilla Parishad Helpline: 1800-000-000\n"
        "• Office hours: Mon–Fri 10:30–17:30\n"
        "• Email: help@zp.example.in\n"
        "_Replace with real contacts before going live._"
    )

def status_lookup(app_id: str) -> str:
    fake = {
        "PMAY123": ("Under review", "Verification within 7 days"),
        "JJM456": ("Approved", "Connection expected in 30 days"),
        "SHG789": ("Pending docs", "Please submit bank passbook copy"),
    }
    st = fake.get(app_id.upper())
    if not st:
        return f"No record found for *{app_id}*. Check the ID and try again."
    return f"*Status for {app_id}:* {st[0]}\nNote: {st[1]}"

def admin_ops(parts):
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

def trivial_intents(text: str, sess: Dict[str, Any]) -> Optional[str]:
    t = normalize(text)

    if t in {"1", "schemes", "scheme", "yojana"}:
        sess["last_intent"] = "schemes"
        return schemes_text()
    if t in {"2", "grievance", "complaint"}:
        sess["last_intent"] = "grievance"
        return (
            "*Grievance (demo)*\n"
            "Reply in this format:\n"
            "`grievance <name> <village> <issue>`\n"
            "Example: `grievance Priya Pal Khadakde water not reaching last mile`\n"
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

    if t.startswith("status "):
        app_id = t.split(" ", 1)[1].strip()
        sess["last_intent"] = "status_lookup"
        return status_lookup(app_id)

    if t.startswith("grievance "):
        payload = text.strip()[len("grievance "):].strip()
        if len(payload) < 4:
            return "Please include details: `grievance <name> <village> <issue>`"
        ticket = f"GRV{int(time.time())%100000:05d}"
        sess["last_intent"] = "grievance_ticket"
        sess["context"]["last_ticket"] = ticket
        return (
            f"Thanks. Ticket *{ticket}* created (demo).\n"
            "You’ll receive an update after initial triage."
        )

    if t.startswith("admin "):
        return admin_ops(t.split())

    if t == "stop":
        SESSIONS.pop(sess.get("id",""), None)
        return "Session cleared. Send ‘menu’ to start again."

    return None

def ai_answer(user_text: str, sess: Dict[str, Any]) -> str:
    """
    AI fallback via Groq or OpenAI (OpenAI-compatible).
    Returns empty string if AI is disabled or unavailable.
    """
    if AI_CLIENT is None or AI_PROVIDER is None:
        return ""

    try:
        system = (
            "You are a helpful district e-governance assistant. "
            "Answer concisely (≤3 sentences) in simple language. "
            "If policies/timelines vary by location, add: "
            "“process may vary by district—verify locally.”"
        )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text}
        ]

        result = AI_CLIENT.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            temperature=0.2,
        )
        return (result.choices[0].message.content or "").strip()
    except Exception as e:
        logging.exception("AI error: %s", e)
        return ""


# ---- Routes ------------------------------------------------------------------
@app.route("/", methods=["GET"])
def health():
    return f"{APP_NAME} is up"

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    wa_from = request.values.get("From", "")
    body = request.values.get("Body", "")
    if not wa_from:
        abort(400)

    sess = session_for(wa_from)
    sess["id"] = wa_from
    sess["history"].append(("user", body))

    msg = trivial_intents(body, sess)
    if not msg:
        msg = ai_answer(body, sess)
    if not msg:
        msg = "I didn’t catch that. Here’s the menu:\n\n" + small_menu()

    sess["history"].append(("bot", msg))
    return reply_text(msg)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
