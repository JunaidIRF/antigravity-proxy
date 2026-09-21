# Antigravity Proxy — Minimal Quickstart & Usage

A local OpenAI-compatible proxy running on `http://127.0.0.1:8877/v1`.

---

## 1. Quick Setup

1. **Sign In**: Double-click `login.bat` (opens browser, sign in with Google once).
2. **Start Server**: Double-click `start_proxy.bat` (or `start_proxy_background.vbs` to run silently).
3. **Stop Server**: Double-click `stop_proxy.bat` when done.

*(Tokens auto-refresh in the background. If you ever need to manually force a token refresh, run `python antigravity_proxy.py --refresh`).*

---

## 2. Configuration (`.env`)

Create a `.env` file in the folder to change settings:

```env
HOST=127.0.0.1
PORT=8877
```
*(Use `HOST=0.0.0.0` to allow other devices on your local Wi-Fi/network to connect).*

---

## 3. Sending a Single Message

### In Windows Command Prompt (CMD):
```cmd
curl http://127.0.0.1:8877/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\": \"gemini-3.8-flash-high\", \"messages\": [{\"role\": \"user\", \"content\": \"Hello, what model are you?\"}]}"
```

### In Python (Standard Library — No Installs):
```python
import json
from urllib.request import Request, urlopen

payload = {
    "model": "gemini-3.8-flash-high",
    "messages": [{"role": "user", "content": "Explain gravity in one sentence."}]
}

req = Request(
    "http://127.0.0.1:8877/v1/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST"
)

with urlopen(req) as resp:
    data = json.loads(resp.read().decode("utf-8"))
    print(data["choices"][0]["message"]["content"])
```

---

## 4. Continuing a Conversation (Multi-Turn Chat)

To continue a chat, include the prior assistant replies and your new message in the `messages` list:

```python
import json
from urllib.request import Request, urlopen

messages = [
    {"role": "user", "content": "My name is John and I live in Tokyo."},
    {"role": "assistant", "content": "Nice to meet you, John! How can I help you today?"},
    {"role": "user", "content": "What is my name and where do I live?"}
]

req = Request(
    "http://127.0.0.1:8877/v1/chat/completions",
    data=json.dumps({"model": "gemini-3.8-flash-high", "messages": messages}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST"
)

with urlopen(req) as resp:
    data = json.loads(resp.read().decode("utf-8"))
    print(data["choices"][0]["message"]["content"])
```

---

## 5. Available Models

### Gemini 3.8 Flash (Flagship)
- `gemini-3.8-flash-high` *(Deep reasoning)*
- `gemini-3.8-flash-medium` *(Balanced / default)*
- `gemini-3.8-flash-low` *(Fast reasoning)*
- `gemini-3.8-flash` *(Alias for medium)*

### Gemini 3.7 Flash
- `gemini-3.7-flash-high` *(Deep reasoning)*
- `gemini-3.7-flash-medium` *(Balanced)*
- `gemini-3.7-flash-low` *(Fast reasoning)*
- `gemini-3.7-flash` *(Alias for medium)*

### Gemini 3.6 Flash
- `gemini-3.6-flash-high` *(Deep reasoning)*
- `gemini-3.6-flash-medium` *(Balanced)*
- `gemini-3.6-flash-low` *(Fast reasoning)*
- `gemini-3.6-flash` *(Alias for medium)*

### Gemini 3.1 Pro
- `gemini-3.1-pro-high` *(Pro model — deep reasoning)*
- `gemini-3.1-pro-low` *(Pro model — fast reasoning / default)*
- `gemini-3.1-pro` *(Alias for low)*

### Claude & Other Frontier Models
- `claude-sonnet-4.6` *(or `claude-sonnet-4.6-thinking`)*
- `claude-opus-4.6` *(or `claude-opus-4.6-thinking`)*
- `gpt-oss-120b` *(or `gpt-oss-120b-medium`)*

---

## 6. Testing Images & PDFs (Multimodal)

Run the included interactive multimodal tester:

```cmd
python test_image_chat.py
```

It prompts you to drag-and-drop any image (PNG, JPG, WEBP, GIF) or PDF, enter your prompt, and prints the model's analysis.
