import os
import json
import logging
from datetime import datetime, timedelta, timezone

import redis
from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from apscheduler.schedulers.background import BackgroundScheduler
import anthropic


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Anthropic
anthropic_client = anthropic.Anthropic(
    api_key=os.environ["ANTHROPIC_API_KEY"]
)

# Redis
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# Twilio
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM")  # ex: whatsapp:+14155238886

twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

SYSTEM_PROMPT = """
Você é um agente pessoal de lembretes e tarefas em português brasileiro.
Ajude o usuário a lembrar de pagar contas e fazer tarefas via WhatsApp.

Você mantém uma lista de tarefas/contas. Quando o usuário mencionar uma nova tarefa ou conta, adicione-a.
Quando marcar como feita, atualize.

IMPORTANTE:
- O sistema PODE enviar notificações automáticas no WhatsApp se a tarefa tiver horário de lembrete.
- Nunca diga que você não consegue lembrar automaticamente.
- Se o usuário pedir um lembrete com horário (ex: "me lembra de pagar a luz às 13:50"),
  salve esse horário no campo "remind_at" em formato ISO 8601.
- Se o usuário não informar horário, "remind_at" deve ser null.
- Considere "hoje", "amanhã", horários como "às 14h", "às 13:50", etc.
- Se não houver data explícita, assuma hoje; se o horário já tiver passado, assuma amanhã.
- Sempre responda em JSON válido.

Formato obrigatório:
{
  "reply": "resposta amigável e direta ao usuário",
  "tasks": [
    {
      "id": número único,
      "name": "nome curto da tarefa",
      "type": "bill" ou "task",
      "urgent": true,
      "done": false,
      "detail": "detalhe opcional",
      "remind_at": "2026-04-06T13:50:00+00:00" ou null,
      "notified": false
    }
  ]
}

Regras:
- Use português brasileiro
- Use emojis moderadamente
- Ao adicionar tarefas, confirme o que foi adicionado
- Mantenha SEMPRE a lista completa atualizada no campo "tasks"
- Se urgente (hoje/amanhã), marque urgent: true
- Contas/boletos = type "bill", demais = type "task"
- Quando listar, mostre as pendentes organizadas
- Quando marcar como concluída, mantenha done=true
- Tarefas concluídas não devem ser notificadas
- Ao criar nova tarefa com lembrete, notified deve começar como false

Comandos úteis:
- "listar" → mostra tarefas pendentes
- "feito: [nome]" → marca tarefa como concluída
- "urgente: [nome]" → marca como urgente
- "ajuda" → mostra comandos
""".strip()


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


def normalize_task(task: dict) -> dict:
    return {
        "id": task.get("id"),
        "name": task.get("name", "").strip(),
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

    # mantém histórico curto
    if len(state.get("history", [])) > 20:
        state["history"] = state["history"][-20:]

    # normaliza tasks
    state["tasks"] = [normalize_task(t) for t in state.get("tasks", [])]

    try:
        redis_client.set(key, json.dumps(state, ensure_ascii=False))
        redis_client.sadd(all_users_set_key(), phone)
    except Exception as e:
        logger.exception("Erro ao salvar estado no Redis para %s: %s", phone, e)


def send_whatsapp_message(to_number: str, body: str) -> None:
    if not twilio_client:
        logger.warning("Twilio client não configurado. Mensagem não enviada para %s", to_number)
        return

    if not TWILIO_WHATSAPP_FROM:
        logger.warning("TWILIO_WHATSAPP_FROM não configurado. Mensagem não enviada para %s", to_number)
        return

    twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_FROM,
        to=to_number,
        body=body
    )
    logger.info("Mensagem enviada para %s", to_number)


def parse_anthropic_json(raw_text: str) -> dict | None:
    try:
        clean = raw_text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except Exception:
        return None


def build_task_context(tasks: list[dict]) -> str:
    return "\n\nLista atual de tarefas do usuário:\n" + json.dumps(tasks, ensure_ascii=False, indent=2)


def process_user_message(incoming_msg: str, phone: str) -> str:
    state = get_user_state(phone)
    state["history"].append({"role": "user", "content": incoming_msg})

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT + build_task_context(state["tasks"]),
            messages=state["history"]
        )

        raw = response.content[0].text
        parsed = parse_anthropic_json(raw)

        if parsed:
            reply_text = parsed.get("reply", "Tudo certo 👍")
            tasks = parsed.get("tasks")

            if isinstance(tasks, list):
                state["tasks"] = [normalize_task(t) for t in tasks]
        else:
            reply_text = raw

        state["history"].append({"role": "assistant", "content": raw})
        save_user_state(phone, state)
        return reply_text

    except Exception as e:
        logger.exception("Erro ao processar mensagem do usuário %s: %s", phone, e)
        return f"Erro ao processar sua mensagem: {str(e)}"


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


def check_due_reminders():
    logger.info("Verificando lembretes pendentes...")

    try:
        users = redis_client.smembers(all_users_set_key())
    except Exception as e:
        logger.exception("Erro ao buscar usuários no Redis: %s", e)
        return

    for phone in users:
        state = get_user_state(phone)
        changed = False

        for task in state.get("tasks", []):
            task = normalize_task(task)

            if task["done"]:
                continue

            if task["notified"]:
                continue

            if is_due(task["remind_at"]):
                try:
                    send_whatsapp_message(phone, build_reminder_text(task))
                    task["notified"] = True
                    changed = True
                except Exception as e:
                    logger.exception("Erro enviando lembrete para %s: %s", phone, e)

        if changed:
            state["tasks"] = [normalize_task(t) for t in state["tasks"]]
            # garante atualização dos campos modificados
            updated_tasks = []
            original_tasks = state.get("tasks", [])

            for original in original_tasks:
                normalized = normalize_task(original)
                if normalized["done"] or normalized["notified"]:
                    updated_tasks.append(normalized)
                else:
                    updated_tasks.append(normalized)

            # melhor abordagem: já reaproveitar os objetos atuais do state
            state["tasks"] = original_tasks
            save_user_state(phone, state)


def check_due_reminders_fixed():
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


@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    from_number = request.form.get("From", "").strip()

    reply_text = process_user_message(incoming_msg, from_number)

    resp = MessagingResponse()
    resp.message(reply_text)
    return str(resp)


@app.route("/status", methods=["GET"])
def status():
    redis_ok = True
    try:
        redis_client.ping()
    except Exception:
        redis_ok = False

    return jsonify({
        "status": "ok",
        "redis": redis_ok,
        "scheduler": "running"
    })


# Scheduler
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(check_due_reminders_fixed, "interval", minutes=1, id="check_due_reminders")


def start_scheduler():
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler iniciado.")


start_scheduler()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
