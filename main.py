import os
import json
import logging
from datetime import datetime, timezone

import redis
from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from apscheduler.schedulers.background import BackgroundScheduler
import anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# =========================
# CONFIG
# =========================
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM")  # ex: whatsapp:+14155238886

MAX_TWILIO_MESSAGE_LEN = 1500

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

SYSTEM_PROMPT = """
Você é um agente pessoal de lembretes e tarefas em português brasileiro.
Ajude o usuário a lembrar de pagar contas e fazer tarefas via WhatsApp.

Você SEMPRE deve usar a ferramenta update_tasks para responder.
Nunca responda diretamente ao usuário sem chamar a ferramenta.

Regras:
- A ferramenta recebe a resposta amigável para o usuário no campo "reply"
- A ferramenta recebe a lista COMPLETA atualizada no campo "tasks"
- Quando o usuário mencionar nova tarefa ou conta, adicione
- Quando marcar como feita, atualize done=true
- Quando pedir lista, organize no texto do campo reply
- Se urgente (hoje/amanhã), marque urgent=true
- Contas/boletos = type "bill", demais = type "task"
- Se houver horário de lembrete, preencha remind_at em ISO 8601
- Se não houver horário, remind_at = null
- Tarefas concluídas não devem ser notificadas
- Tarefas novas com lembrete devem começar com notified=false
- Responda sempre em português brasileiro
- Use emojis com moderação
""".strip()

TOOLS = [
    {
        "name": "update_tasks",
        "description": "Atualiza a lista de tarefas e define a resposta que será enviada ao usuário no WhatsApp.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reply": {
                    "type": "string",
                    "description": "Mensagem amigável e curta para o usuário."
                },
                "tasks": {
                    "type": "array",
                    "description": "Lista completa e atualizada de tarefas do usuário.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "name": {"type": "string"},
                            "type": {"type": "string", "enum": ["bill", "task"]},
                            "urgent": {"type": "boolean"},
                            "done": {"type": "boolean"},
                            "detail": {"type": ["string", "null"]},
                            "remind_at": {"type": ["string", "null"]},
                            "notified": {"type": "boolean"}
                        },
                        "required": [
                            "id",
                            "name",
                            "type",
                            "urgent",
                            "done",
                            "detail",
                            "remind_at",
                            "notified"
                        ],
                        "additionalProperties": False
                    }
                }
            },
            "required": ["reply", "tasks"],
            "additionalProperties": False
        },
        "strict": True
    }
]


# =========================
# HELPERS
# =========================
def now_utc():
    return datetime.now(timezone.utc)


def safe_json_loads(value, default):
    try:
        return json.loads(value)
    except Exception:
        return default


def get_user_key(phone: str) -> str:
    return f"user:{phone}"


def all_users_set_key() -> str:
    return "users:all"


def split_message(text: str, max_length: int = MAX_TWILIO_MESSAGE_LEN) -> list[str]:
    if not text:
        return [""]

    text = str(text).strip()
    parts = []

    while len(text) > max_length:
        split_index = text.rfind("\n", 0, max_length)

        if split_index == -1 or split_index < int(max_length * 0.5):
            split_index = text.rfind(" ", 0, max_length)

        if split_index == -1 or split_index < int(max_length * 0.5):
            split_index = max_length

        chunk = text[:split_index].strip()
        if chunk:
            parts.append(chunk)

        text = text[split_index:].strip()

    if text:
        parts.append(text)

    return parts


def add_part_prefix(parts: list[str]) -> list[str]:
    if len(parts) <= 1:
        return parts
    total = len(parts)
    return [f"Parte {i}/{total}\n{part}" for i, part in enumerate(parts, start=1)]


def normalize_task(task: dict) -> dict:
    return {
        "id": int(task.get("id", 0)),
        "name": str(task.get("name", "")).strip(),
        "type": task.get("type", "task"),
        "urgent": bool(task.get("urgent", False)),
        "done": bool(task.get("done", False)),
        "detail": task.get("detail", "") or "",
        "remind_at": task.get("remind_at"),
        "notified": bool(task.get("notified", False)),
    }


def get_user_state(phone: str) -> dict:
    key = get_user_key(phone)
    try:
        data = redis_client.get(key)
        if data:
            state = safe_json_loads(data, {"history": [], "tasks": []})
            state.setdefault("history", [])
            state.setdefault("tasks", [])
            return state
    except Exception as e:
        logger.exception("Erro ao ler estado do Redis para %s: %s", phone, e)

    return {"history": [], "tasks": []}


def save_user_state(phone: str, state: dict) -> None:
    key = get_user_key(phone)

    if len(state.get("history", [])) > 20:
        state["history"] = state["history"][-20:]

    state["tasks"] = [normalize_task(t) for t in state.get("tasks", [])]

    try:
        redis_client.set(key, json.dumps(state, ensure_ascii=False))
        redis_client.sadd(all_users_set_key(), phone)
    except Exception as e:
        logger.exception("Erro ao salvar estado no Redis para %s: %s", phone, e)


def build_task_context(tasks: list[dict]) -> str:
    return "Lista atual de tarefas do usuário:\n" + json.dumps(tasks, ensure_ascii=False, indent=2)


def is_due(remind_at: str | None) -> bool:
    if not remind_at:
        return False

    try:
        dt = datetime.fromisoformat(remind_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= now_utc()
    except Exception:
        return False


def build_reminder_text(task: dict) -> str:
    name = task.get("name", "tarefa")
    detail = task.get("detail", "")
    prefix = "💸 Lembrete de conta" if task.get("type") == "bill" else "⏰ Lembrete"

    if detail:
        return f"{prefix}: {name}\n{detail}\n\nResponda 'feito: {name}' quando concluir ✅"
    return f"{prefix}: {name}\n\nResponda 'feito: {name}' quando concluir ✅"


# =========================
# TWILIO SEND
# =========================
def send_whatsapp_message(to_number: str, body: str) -> None:
    if not twilio_client:
        logger.warning("Twilio client não configurado. Mensagem não enviada para %s", to_number)
        return

    if not TWILIO_WHATSAPP_FROM:
        logger.warning("TWILIO_WHATSAPP_FROM não configurado. Mensagem não enviada para %s", to_number)
        return

    parts = add_part_prefix(split_message(body))

    for part in parts:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=to_number,
            body=part
        )

    logger.info("Mensagem enviada para %s em %s parte(s)", to_number, len(parts))


# =========================
# CLAUDE TOOL USE
# =========================
def extract_tool_use_block(message):
    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "update_tasks":
            return block
    return None


def process_user_message(incoming_msg: str, phone: str) -> str:
    state = get_user_state(phone)

    # histórico em formato simples de texto
    prior_messages = state.get("history", [])
    user_prompt = f"{build_task_context(state['tasks'])}\n\nMensagem do usuário: {incoming_msg}"

    messages = prior_messages + [
        {"role": "user", "content": user_prompt}
    ]

    try:
        first_response = anthropic_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=700,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

        tool_block = extract_tool_use_block(first_response)

        if not tool_block:
            logger.warning("Claude não chamou a ferramenta update_tasks.")
            reply_text = "Entendi sua mensagem, mas tive uma falha ao atualizar suas tarefas 😅"
            state["history"].append({"role": "user", "content": user_prompt})
            state["history"].append({"role": "assistant", "content": reply_text})
            save_user_state(phone, state)
            return reply_text

        tool_input = tool_block.input
        reply_text = str(tool_input.get("reply", "Tudo certo 👍")).strip()
        tasks = tool_input.get("tasks", [])

        if isinstance(tasks, list):
            state["tasks"] = [normalize_task(t) for t in tasks]

        # devolve tool_result para completar o loop corretamente
        second_response = anthropic_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=50,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages + [
                {
                    "role": "assistant",
                    "content": first_response.content
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_block.id,
                            "content": json.dumps({"status": "ok"}, ensure_ascii=False)
                        }
                    ]
                }
            ]
        )

        state["history"].append({"role": "user", "content": user_prompt})
        state["history"].append({"role": "assistant", "content": reply_text})
        save_user_state(phone, state)
        return reply_text

    except Exception as e:
        logger.exception("Erro ao processar mensagem do usuário %s: %s", phone, e)
        return "Erro ao processar sua mensagem 😕"


# =========================
# SCHEDULER
# =========================
def check_due_reminders():
    logger.info("Verificando lembretes pendentes...")

    try:
        users = redis_client.smembers(all_users_set_key())
    except Exception as e:
        logger.exception("Erro ao buscar usuários no Redis: %s", e)
        return

    for phone in users:
        state = get_user_state(phone)
        tasks = [normalize_task(t) for t in state.get("tasks", [])]
        changed = False

        for task in tasks:
            if task["done"] or task["notified"]:
                continue

            if is_due(task["remind_at"]):
                try:
                    send_whatsapp_message(phone, build_reminder_text(task))
                    task["notified"] = True
                    changed = True
                except Exception as e:
                    logger.exception("Erro enviando lembrete para %s: %s", phone, e)

        if changed:
            state["tasks"] = tasks
            save_user_state(phone, state)


# =========================
# ROUTES
# =========================
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    from_number = request.form.get("From", "").strip()

    reply_text = process_user_message(incoming_msg, from_number)

    resp = MessagingResponse()
    parts = add_part_prefix(split_message(reply_text))

    for part in parts:
        resp.message(part)

    return str(resp)


@app.route("/status", methods=["GET"])
def status():
    redis_ok = True
    twilio_ok = bool(twilio_client and TWILIO_WHATSAPP_FROM)

    try:
        redis_client.ping()
    except Exception:
        redis_ok = False

    return jsonify({
        "status": "ok",
        "redis": redis_ok,
        "twilio": twilio_ok,
        "scheduler": "running"
    })


# =========================
# STARTUP
# =========================
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(
    check_due_reminders,
    "interval",
    minutes=1,
    id="check_due_reminders",
    replace_existing=True
)

if not scheduler.running:
    scheduler.start()
    logger.info("Scheduler iniciado.")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
