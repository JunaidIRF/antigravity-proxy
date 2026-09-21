# AGENTS.md — LLM Agent Instructions

> **If you are an AI assistant (Claude, GPT, Gemini, etc.) helping a user set up this proxy, read this file first. It contains everything you need to guide the user through installation, configuration, and troubleshooting.**

---

## What This Project Is

A local proxy server that translates OpenAI-compatible API calls into Google Antigravity's internal Cloud Code Assist API format. It lets any tool that speaks OpenAI format use Gemini and Claude models for free, authenticated via the Antigravity CLI's OAuth token.

The proxy listens on `http://127.0.0.1:8877` and exposes two endpoints:
- `POST /v1/chat/completions` — OpenAI chat format (messages, tools, streaming)
- `POST /v1beta/models/{model}:generateContent` — native Gemini passthrough (images, structured output)

---

## How to Guide a User Through Setup

### Step 1 — Check Prerequisites

Ask the user:
1. **Do you have Python 3.10+?** Check with `python --version`
2. **Authenticate with Google:**
   - **Windows:** Run `login.bat` or `python antigravity_proxy.py --login` (opens browser, signs in, saves tokens automatically).
   - **macOS/Linux:** Run `python3 antigravity_proxy.py --login` or use the Antigravity CLI (`agy`).

Check if token exists with: `python antigravity_proxy.py --check-token`

### Step 2 — Start the Proxy

- **Windows:** Run `start_proxy.bat` (or `start_proxy_background.vbs` to run silently).
- **macOS/Linux:** `python3 antigravity_proxy.py`

Verify it's running:
```bash
curl http://127.0.0.1:8877/health
# Should return: {"status": "ok", "service": "antigravity-proxy", "auth": {"authenticated": true, ...}}
```

### Step 3 — Configure Their Tool

Ask what tool they want to connect. Apply these settings:

| Tool Type         | Base URL                              | API Key         | Model             |
|-------------------|---------------------------------------|-----------------|-------------------|
| OpenAI SDK        | `http://127.0.0.1:8877/v1`            | Any string      | `gemini-3.8-flash` |
| Continue.dev      | `http://127.0.0.1:8877/v1`            | Any string      | `gemini-3.8-flash` |
| Aider             | `--openai-api-base http://127.0.0.1:8877/v1` | Any string | `gemini-3.8-flash` |
| Hermes Agent      | `hermes config set ...`               | Any string      | `gemini-3.8-flash` |
| Claude Code       | `ANTHROPIC_BASE_URL` env var          | N/A             | `claude-sonnet-4.6` |

### Step 4 — Verify It Works

Have them run a test request:
```bash
curl http://127.0.0.1:8877/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "Say hello"}]}'
```

---

## Available Models

```
gemini-3.8-flash        Gemini 3.8 Flash (latest default)
gemini-3.8-flash-high   Gemini 3.8 Flash (thinking: high)
gemini-3.8-flash-low    Gemini 3.8 Flash (thinking: low)
gemini-3.7-flash        Gemini 3.7 Flash
gemini-3.6-flash        Gemini 3.6 Flash
gemini-3.5-flash        Gemini 3.5 Flash
gemini-3.1-pro          Gemini 3.1 Pro (thinking: low)
gemini-3.1-pro-high     Gemini 3.1 Pro (thinking: high)
gemini-3-flash          Gemini 3 Flash
gemini-2.5-pro          Gemini 2.5 Pro
gemini-2.5-flash        Gemini 2.5 Flash
claude-sonnet-4.6       Claude Sonnet 4.6
claude-opus-4.6         Claude Opus 4.6 (thinking enabled)
gpt-oss-120b            Open Source Model 120B
```

---

## Common Issues and Solutions

### "Token file not found"
The user hasn't authenticated with `agy` yet. Run `agy` and sign in.

### "No refresh_token in token file"
The token file is corrupted or incomplete. Delete it and re-run `agy`:
```bash
rm ~/.gemini/antigravity-cli/antigravity-oauth-token
agy  # re-authenticate
```

### 401 / auth errors after working for a while
The OAuth refresh token has expired. Re-run `agy` and restart the proxy.

### Model returns empty response or 404
The model name doesn't exist on the Antigravity backend. Use a name from the model table above. When in doubt, `gemini-3-flash` or `gemini-3.5-flash` are the most reliable.

### Proxy is slow on first request
The first request discovers the project ID (extra network call). Subsequent requests are faster.

### Requests timeout
The Antigravity backend can be slow, especially for thinking models. This is not a proxy issue — it's upstream latency.

---

## Architecture (for context)

```
Client (OpenAI format)
  → Proxy translates to Gemini format
  → Wraps in Antigravity envelope
  → Forwards to cloudcode-pa.googleapis.com
  → Response unwrapped, converted to OpenAI format
  → Returned to client
```

The proxy auto-refreshes the OAuth token when it expires. It uses Python stdlib only — no dependencies to install.

---

## File Reference

| File                        | Purpose                                          |
|-----------------------------|--------------------------------------------------|
| `antigravity_proxy.py`      | Main proxy server (single file, stdlib only)     |
| `antigravity-proxy.service` | systemd user service template                    |
| `README.md`                 | Full human-readable documentation                |
| `AGENTS.md`                 | This file — instructions for AI assistants       |

---

## Security Notes

- The proxy binds to `127.0.0.1` by default — only accessible locally
- No API key or authentication layer (relies on localhost binding)
- OAuth token stays on the user's machine, never sent anywhere except Google
- The `CLIENT_ID` and `CLIENT_SECRET` constants are the same values embedded in every Antigravity CLI binary — they are not personal secrets
