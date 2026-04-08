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

# Use um modelo ativo da sua conta. Se preferir, troque por outro disponível para você.
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")

SYSTEM_PROMPT = """
Você é um agente pessoal de lembretes e tarefas em português brasileiro no WhatsApp.

Sua função é ENTENDER a intenção do usuário e chamar a ferramenta `task_action`.

Regras importantes:
- Nunca responda diretamente ao usuário sem chamar a ferramenta `task_action`.
- Você NÃO deve reescrever a agenda inteira manualmente.
- Você deve informar apenas a ação desejada:
  - add_task
  - mark_done
  - mark_urgent
  - delete_task
  - list_tasks
  - noop
- Para ações em item existente, use `target_name` com o nome mais provável da tarefa.
- Para adicionar tarefa, preencha `task`.
- Para listar, use action=list_tasks.
- Para mensagens como "feito: X", normalmente use action=mark_done.
- Para mensagens como "urgente: X", normalmente use action=mark_urgent.
- Para mensagens ambíguas, use noop.
- Responda sempre em português brasileiro.
- Use emojis com moderação.
- Seja útil e breve.

Sobre tarefas:
- type = "bill" para contas/boletos
- type = "task" para demais tarefas
- urgent = true se claramente hoje/amanhã ou o usuário disser que é urgente
- done = false ao criar nova tarefa
- detail = detalhe curto opcional
- remind_at = ISO 8601 se houver horário explícito de lembrete, senão null
- notified = false em tarefa nova
""".strip()

TOOLS = [
    {
        "name": "task_action",
        "description": "Define a ação a ser aplicada sobre a agenda do usuário.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reply": {
                    "type": "string",
                    "description": "Mensagem amigável que será enviada ao usuário no WhatsApp."
                },
                "action": {
                    "type": "string",
                    "enum": [
                        "add_task",
                        "mark_done",
                        "mark_urgent",
                        "delete_task",
                        "list_tasks",
                        "noop"
                    ]
                },
                "target_name": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "null"}
                    ],
                    "description": "Nome da tarefa alvo para editar/remover/marcar."
                },
                "task": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "type": {"type": "string", "enum": ["bill", "task"]},
                                "urgent": {"type": "boolean"},
                                "done": {"type": "boolean"},
                                "detail": {
                                    "anyOf": [
                                        {"type": "string"},
                                        {"type": "null"}
                                    ]
                                },
                                "remind_at": {
                                    "anyOf": [
                                        {"type": "string"},
                                        {"type": "null"}
                                    ]
                                },
                                "notified": {"type": "boolean"}
                            },
                            "required": [
                                "name",
                                "type",
                                "urgent",
                                "done",
                                "detail",
                                "remind_at",
                                "notified"
                            ],
                            "additionalProperties": False
                        },
                        {"type": "null"}
                    ]
                }
            },
            "required": ["reply", "action", "target_name", "task"],
            "additionalProperties": False
        }
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


def format_task_list(tasks: list[dict]) -> str:
    pending = [t for t in tasks if not t["done"]]

    if not pending:
        return "Sua agenda está vazia no momento 😊"

    urgent = [t for t in pending if t["urgent"]]
    recurring = [t for t in pending if any(word in (t["detail"] or "").lower() for word in ["todo", "toda", "segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"])]
    bills = [t for t in pending if t["type"] == "bill"]
    normal = [t for t in pending if t not in urgent and t not in recurring and t not in bills]

    lines = ["📋 *Sua Agenda:*", ""]

    if urgent:
        lines.append("🔥 *Urgente:*")
        for t in urgent:
            detail = f" - {t['detail']}" if t["detail"] else ""
            lines.append(f"• {t['name']}{detail}")
        lines.append("")

    if normal:
        lines.append("📝 *Tarefas:*")
        for t in normal:
            detail = f" - {t['detail']}" if t["detail"] else ""
            lines.append(f"• {t['name']}{detail}")
        lines.append("")

    if recurring:
        lines.append("🔁 *Recorrentes:*")
        for t in recurring:
            detail = f" - {t['detail']}" if t["detail"] else ""
            lines.append(f"• {t['name']}{detail}")
        lines.append("")

    if bills:
        lines.append("💸 *Contas:*")
        for t in bills:
            detail = f" - {t['detail']}" if t["detail"] else ""
            lines.append(f"• {t['name']}{detail}")

    return "\n".join(lines).strip()


def find_task_index(tasks: list[dict], target_name: str) -> int:
    target = target_name.strip().lower()
    if not target:
        return -1

    # Match exato
    for i, task in enumerate(tasks):
        if task["name"].strip().lower() == target:
            return i

    # Contido
    for i, task in enumerate(tasks):
        name = task["name"].strip().lower()
        if target in name or name in target:
            return i

    return -1


def next_task_id(tasks: list[dict]) -> int:
    ids = [t["id"] for t in tasks if isinstance(t.get("id"), int)]
    return max(ids, default=0) + 1


def apply_action_to_state(state: dict, tool_input: dict) -> str:
    reply = str(tool_input.get("reply", "Tudo certo 👍")).strip() or "Tudo certo 👍"
    action = tool_input.get("action", "noop")
    target_name = (tool_input.get("target_name") or "").strip()
    new_task = tool_input.get("task")

    tasks = [normalize_task(t) for t in state.get("tasks", [])]

    if action == "mark_done":
        idx = find_task_index(tasks, target_name)
        if idx == -1:
            return "Não encontrei essa tarefa na sua agenda 😕"
        tasks[idx]["done"] = True
        tasks[idx]["notified"] = True

    elif action == "mark_urgent":
        idx = find_task_index(tasks, target_name)
        if idx == -1:
            return "Não encontrei essa tarefa para marcar como urgente 😕"
        tasks[idx]["urgent"] = True

    elif action == "delete_task":
        idx = find_task_index(tasks, target_name)
        if idx == -1:
            return "Não encontrei essa tarefa para remover 😕"
        tasks.pop(idx)

    elif action == "add_task":
        if not isinstance(new_task, dict):
            return "Entendi que você quer adicionar algo, mas faltaram detalhes 😕"

        normalized = normalize_task(new_task)
        if not normalized["name"]:
            return "Não consegui identificar o nome da tarefa 😕"

        normalized["id"] = next_task_id(tasks)

        # Se já existir algo muito parecido e ainda pendente, evita duplicar
        existing_idx = find_task_index(tasks, normalized["name"])
        if existing_idx != -1 and not tasks[existing_idx]["done"]:
            return "Essa tarefa já está na sua agenda 😉"

        tasks.append(normalized)

    elif action == "list_tasks":
        reply = format_task_list(tasks)

    elif action == "noop":
        pass

    state["tasks"] = tasks
    return reply


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
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "task_action":
            return block
    return None


def process_user_message(incoming_msg: str, phone: str) -> str:
    state = get_user_state(phone)

    messages = [
        {
            "role": "user",
            "content": f"{build_task_context(state['tasks'])}\n\nMensagem do usuário: {incoming_msg}"
        }
    ]

    try:
        response = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

        tool_block = extract_tool_use_block(response)

        if not tool_block:
            logger.warning("Claude não chamou a ferramenta task_action.")
            reply_text = "Entendi sua mensagem, mas tive uma falha ao atualizar sua agenda 😅"
            state["history"].append({"role": "user", "content": incoming_msg})
            state["history"].append({"role": "assistant", "content": reply_text})
            save_user_state(phone, state)
            return reply_text

        reply_text = apply_action_to_state(state, tool_block.input)

        state["history"].append({"role": "user", "content": incoming_msg})
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
