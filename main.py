import os
import json
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import anthropic

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Armazena histórico e tarefas por número de telefone
usuarios = {}

SYSTEM_PROMPT = """Você é um agente pessoal de lembretes e tarefas em português brasileiro.
Ajude o usuário a lembrar de pagar contas e fazer tarefas via WhatsApp.

Você mantém uma lista de tarefas/contas. Quando o usuário mencionar uma nova tarefa ou conta, adicione-a.
Quando marcar como feita, atualize.

IMPORTANTE: Sempre responda em JSON válido no seguinte formato:
{
  "reply": "sua resposta amigável e direta ao usuário (use emojis, é WhatsApp!)",
  "tasks": [lista completa e atualizada de todas as tarefas]
}

Cada tarefa deve ter:
{
  "id": número único,
  "name": "nome curto da tarefa",
  "type": "bill" ou "task",
  "urgent": true/false,
  "done": true/false,
  "detail": "detalhe opcional (data, valor, etc)"
}

Regras:
- Seja direto, amigável e use linguagem natural brasileira
- Use emojis moderadamente (é WhatsApp)
- Ao adicionar tarefas, confirme o que foi adicionado
- Mantenha SEMPRE a lista completa atualizada no campo "tasks"
- Se urgente (hoje/amanhã), marque urgent: true
- Contas/boletos = type "bill", demais = type "task"
- Quando o usuário pedir lista, mostre as pendentes de forma organizada
- Comandos úteis que o usuário pode usar:
  "listar" → mostra todas as tarefas pendentes
  "feito: [nome]" → marca tarefa como concluída
  "urgente: [nome]" → marca como urgente
  "ajuda" → mostra os comandos disponíveis
"""

def get_user_state(phone):
    if phone not in usuarios:
        usuarios[phone] = {"history": [], "tasks": []}
    return usuarios[phone]

def format_task_list(tasks):
    """Formata lista de tarefas para exibição em texto simples."""
    pending = [t for t in tasks if not t.get("done")]
    done = [t for t in tasks if t.get("done")]

    lines = []
    if pending:
        lines.append(f"📋 *Pendentes ({len(pending)}):*")
        for t in pending:
            icon = "🔴" if t.get("urgent") else ("💰" if t.get("type") == "bill" else "✅")
            detail = f" — {t['detail']}" if t.get("detail") else ""
            lines.append(f"  {icon} {t['name']}{detail}")
    if done:
        lines.append(f"\n✅ *Concluídas ({len(done)}):*")
        for t in done:
            lines.append(f"  ~~{t['name']}~~")

    return "\n".join(lines) if lines else "Nenhuma tarefa ainda!"

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    from_number = request.form.get("From", "")

    state = get_user_state(from_number)
    task_context = f"\n\nLista atual de tarefas do usuário: {json.dumps(state['tasks'], ensure_ascii=False)}"

    state["history"].append({"role": "user", "content": incoming_msg})

    # Mantém histórico em no máximo 20 mensagens para não estourar contexto
    if len(state["history"]) > 20:
        state["history"] = state["history"][-20:]

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT + task_context,
            messages=state["history"]
        )

        raw = response.content[0].text

        try:
            clean = raw.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean)
            reply_text = parsed.get("reply", raw)
            if parsed.get("tasks") is not None:
                state["tasks"] = parsed["tasks"]
        except Exception:
            reply_text = raw

        state["history"].append({"role": "assistant", "content": raw})

    except Exception as e:
        reply_text = f"Erro ao processar sua mensagem: {str(e)}"

    resp = MessagingResponse()
    resp.message(reply_text)
    return str(resp)

@app.route("/status", methods=["GET"])
def status():
    return {"status": "ok", "usuarios_ativos": len(usuarios)}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)


