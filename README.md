# 🤖 Agente de Lembretes — WhatsApp + Claude

Agente pessoal de lembretes e tarefas via WhatsApp, usando a API do Claude (Anthropic) e Twilio.

---

## Pré-requisitos

- Python 3.9+
- Conta na [Anthropic](https://console.anthropic.com) (para a chave de API)
- Conta na [Twilio](https://www.twilio.com) (gratuita para teste)
- [ngrok](https://ngrok.com) instalado (para expor o servidor localmente)

---

## Passo 1 — Instalar dependências

```bash
pip install -r requirements.txt
```

---

## Passo 2 — Configurar variáveis de ambiente

Copie o arquivo de exemplo e preencha:

```bash
cp .env.example .env
```

Edite o `.env`:
```
ANTHROPIC_API_KEY=sk-ant-...     ← console.anthropic.com → API Keys
TWILIO_ACCOUNT_SID=AC...         ← console.twilio.com → Account Info
TWILIO_AUTH_TOKEN=...            ← console.twilio.com → Account Info
```

---

## Passo 3 — Configurar Twilio WhatsApp Sandbox

1. Acesse [console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn](https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn)
2. Anote o número do Sandbox (ex: `+1 415 523 8886`)
3. No seu WhatsApp, mande uma mensagem para esse número com o código de ativação que aparece na tela (ex: `join silver-fox`)
4. Você receberá uma confirmação de que está no Sandbox

---

## Passo 4 — Rodar o servidor

```bash
# Carregar variáveis de ambiente
export $(cat .env | xargs)

# Iniciar o servidor
python server.py
```

O servidor vai rodar em `http://localhost:5000`

---

## Passo 5 — Expor com ngrok

Em outro terminal:

```bash
ngrok http 5000
```

Copie a URL gerada, parecida com:
```
https://abc123.ngrok-free.app
```

---

## Passo 6 — Configurar Webhook no Twilio

1. No painel Twilio, vá em **Messaging → Try it out → Send a WhatsApp message**
2. No campo **"When a message comes in"**, cole:
   ```
   https://abc123.ngrok-free.app/webhook
   ```
3. Método: **HTTP POST**
4. Salve

---

## Testando

Mande uma mensagem para o número do Sandbox no WhatsApp:

```
lembra de pagar o condomínio dia 10
```

O agente vai responder e adicionar à lista de tarefas!

---

## Comandos que o agente entende

| Comando | O que faz |
|---------|-----------|
| `listar` | Mostra todas as tarefas pendentes |
| `feito: conta de luz` | Marca tarefa como concluída |
| `urgente: boleto` | Marca como urgente |
| `ajuda` | Lista os comandos disponíveis |
| Qualquer texto natural | O agente entende e responde |

---

## Deploy em produção (opcional)

Para rodar 24/7 sem precisar do ngrok, faça deploy no [Railway](https://railway.app) (gratuito):

```bash
# Instale o CLI do Railway
npm install -g @railway/cli

# Login e deploy
railway login
railway init
railway up
```

Depois atualize a URL do Webhook no Twilio para a URL do Railway.

---

## Estrutura do projeto

```
agente-whatsapp/
├── server.py          ← servidor principal
├── requirements.txt   ← dependências Python
├── .env.example       ← template de variáveis
├── .env               ← suas chaves (não subir para o git!)
└── README.md
```

---

## Importante

- Adicione `.env` no `.gitignore` — nunca suba suas chaves para o GitHub
- O histórico de conversa fica em memória (reiniciar o servidor apaga)
- Para persistência, substitua o dicionário `usuarios` por um banco de dados (SQLite, Redis, etc.)
