# API Integration Guide for AI Agents & Developers

This document provides complete instructions, API specifications, and code patterns for AI coding agents building applications using this local OpenAI-compatible API backend.

---

## 1. Overview & Connection Info

The API backend is already hosted and running locally:

| Parameter | Value |
|---|---|
| **Base URL** | `http://127.0.0.1:8877/v1` |
| **Chat Endpoint** | `POST http://127.0.0.1:8877/v1/chat/completions` |
| **Models Endpoint** | `GET http://127.0.0.1:8877/v1/models` |
| **API Key** | Any string (e.g. `dummy` or `not-needed`) |
| **Format** | Standard OpenAI Chat Completions JSON |

### Environment Variables for Applications
When configuring an application or LLM client library (OpenAI SDK, LangChain, LlamaIndex, Vercel AI SDK, etc.), set:
```env
OPENAI_BASE_URL=http://127.0.0.1:8877/v1
OPENAI_API_KEY=not-needed
```

---

## 2. Available Models & Reasoning Controls

All Gemini models support text, vision (images), PDF document analysis, and function calling. Select the appropriate model based on speed and reasoning depth:

### Gemini 3.8 Flash (Recommended Flagship)
- `gemini-3.8-flash-high` — Maximum reasoning / deep thinking (complex logic, coding, debugging)
- `gemini-3.8-flash-medium` — Balanced reasoning and speed (default for `gemini-3.8-flash`)
- `gemini-3.8-flash-low` — Fast reasoning with lower latency

### Gemini 3.7 Flash
- `gemini-3.7-flash-high` — Deep reasoning
- `gemini-3.7-flash-medium` — Balanced reasoning
- `gemini-3.7-flash-low` — Fast reasoning

### Gemini 3.6 Flash
- `gemini-3.6-flash-high` — Deep reasoning
- `gemini-3.6-flash-medium` — Balanced reasoning
- `gemini-3.6-flash-low` — Fast reasoning

### Gemini 3.1 Pro (Heavyweight Reasoning)
- `gemini-3.1-pro-high` — Maximum reasoning depth for challenging architectures & math
- `gemini-3.1-pro-low` — Standard pro reasoning (default for `gemini-3.1-pro`)

### Claude & Open-Source Frontier Models
- `claude-sonnet-4.6` *(or `claude-sonnet-4.6-thinking`)* — High capability for synthesis & coding
- `claude-opus-4.6` *(or `claude-opus-4.6-thinking`)* — Deepest reasoning Claude model
- `gpt-oss-120b` — Open-source 120B model

---

## 3. Basic Chat Completions (Single Message)

### Python (Standard Library — Zero Dependencies)
```python
import json
from urllib.request import Request, urlopen

payload = {
    "model": "gemini-3.8-flash-high",
    "messages": [
        {"role": "system", "content": "You are a concise software engineering assistant."},
        {"role": "user", "content": "Explain quicksort in 2 bullet points."}
    ]
}

req = Request(
    "http://127.0.0.1:8877/v1/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer not-needed"
    },
    method="POST"
)

with urlopen(req) as resp:
    result = json.loads(resp.read().decode("utf-8"))
    reply = result["choices"][0]["message"]["content"]
    print(reply)
```

### Python (`openai` Official SDK)
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8877/v1",
    api_key="not-needed"
)

response = client.chat.completions.create(
    model="gemini-3.8-flash-high",
    messages=[
        {"role": "user", "content": "Write a python function to compute fibonacci."}
    ]
)

print(response.choices[0].message.content)
```

### TypeScript / JavaScript (`fetch`)
```typescript
async function askAI(prompt: string) {
  const response = await fetch("http://127.0.0.1:8877/v1/chat/completions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": "Bearer not-needed"
    },
    body: JSON.stringify({
      model: "gemini-3.8-flash-high",
      messages: [{ role: "user", content: prompt }]
    })
  });

  const data = await response.json();
  return data.choices[0].message.content;
}
```

---

## 4. Multi-Turn Conversation (Continuing a Chat)

To maintain conversation context across multiple turns, append previous messages (both `user` and `assistant`) to the `messages` array:

```python
import json
from urllib.request import Request, urlopen

conversation_history = [
    {"role": "system", "content": "You are a helpful travel assistant."},
    {"role": "user", "content": "I want to visit Tokyo for 3 days. What are the top areas to stay?"},
    {"role": "assistant", "content": "Shinjuku, Shibuya, and Ginza are the best areas for convenience and transport."},
    {"role": "user", "content": "Which of those three is best if I love nightlife and street fashion?"}
]

req = Request(
    "http://127.0.0.1:8877/v1/chat/completions",
    data=json.dumps({
        "model": "gemini-3.8-flash-medium",
        "messages": conversation_history
    }).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST"
)

with urlopen(req) as resp:
    data = json.loads(resp.read().decode("utf-8"))
    assistant_reply = data["choices"][0]["message"]["content"]
    print(assistant_reply)

    # Append assistant's answer to keep extending the conversation
    conversation_history.append({"role": "assistant", "content": assistant_reply})
```

---

## 5. Multimodal: Images, PDFs, and Documents

The API accepts media directly through standard OpenAI-style data URIs (`data:<mime-type>;base64,<encoded-data>`).

### Supported Formats
- **Images**: PNG (`image/png`), JPEG (`image/jpeg`), WEBP (`image/webp`), GIF (`image/gif`), HEIC (`image/heic`)
- **Documents**: PDF (`application/pdf`)
- **Audio**: MP3 (`audio/mp3`), WAV (`audio/wav`), MPEG (`audio/mpeg`)

### Python: Send an Image or PDF
```python
import base64
import json
import mimetypes
from urllib.request import Request, urlopen

def send_multimodal(file_path: str, prompt: str, model: str = "gemini-3.8-flash-high") -> str:
    # 1. Detect MIME type
    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = "application/pdf" if file_path.lower().endswith(".pdf") else "image/png"

    # 2. Read and encode file as base64 data URI
    with open(file_path, "rb") as f:
        b64_data = base64.b64encode(f.read()).decode("utf-8")
    data_uri = f"data:{mime_type};base64,{b64_data}"

    # 3. Construct OpenAI multimodal payload
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}}
                ]
            }
        ]
    }

    req = Request(
        "http://127.0.0.1:8877/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    with urlopen(req, timeout=120) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        return res["choices"][0]["message"]["content"]

# Example usage with an image:
# print(send_multimodal("invoice.png", "Extract the total amount and invoice date as JSON."))

# Example usage with a PDF:
# print(send_multimodal("whitepaper.pdf", "Summarize the key architectural findings."))
```

### TypeScript / Node.js: Send an Image or PDF
```typescript
import * as fs from "fs";
import * as path from "path";

async function analyzeFile(filePath: string, prompt: string) {
  const ext = path.extname(filePath).toLowerCase();
  const mimeMap: Record<string, string> = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
  };
  const mimeType = mimeMap[ext] || "image/png";

  const fileBase64 = fs.readFileSync(filePath).toString("base64");
  const dataUri = `data:${mimeType};base64,${fileBase64}`;

  const response = await fetch("http://127.0.0.1:8877/v1/chat/completions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "gemini-3.8-flash-high",
      messages: [
        {
          role: "user",
          content: [
            { type: "text", text: prompt },
            { type: "image_url", image_url: { url: dataUri } }
          ]
        }
      ]
    })
  });

  const data = await response.json();
  return data.choices[0].message.content;
}
```

---

## 6. Streaming Responses (`stream: true`)

Streaming returns Server-Sent Events (SSE) chunks formatted as `data: { ... }`.

### Python Streaming Example
```python
import json
from urllib.request import Request, urlopen

payload = {
    "model": "gemini-3.8-flash-high",
    "messages": [{"role": "user", "content": "Write a 100-word story about an astronaut."}],
    "stream": True
}

req = Request(
    "http://127.0.0.1:8877/v1/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST"
)

with urlopen(req) as resp:
    for line in resp:
        line = line.decode("utf-8").strip()
        if not line or not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
            delta = chunk["choices"][0]["delta"].get("content", "")
            print(delta, end="", flush=True)
        except json.JSONDecodeError:
            continue
print()
```

---

## 7. Function Calling / Tools

You can declare tools using the standard OpenAI JSON Schema:

```python
import json
from urllib.request import Request, urlopen

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current temperature and conditions for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "The city name"}
                },
                "required": ["city"]
            }
        }
    }
]

payload = {
    "model": "gemini-3.8-flash-medium",
    "messages": [{"role": "user", "content": "What is the weather in Tokyo?"}],
    "tools": tools,
    "tool_choice": "auto"
}

req = Request(
    "http://127.0.0.1:8877/v1/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST"
)

with urlopen(req) as resp:
    data = json.loads(resp.read().decode("utf-8"))
    msg = data["choices"][0]["message"]
    if "tool_calls" in msg and msg["tool_calls"]:
        for tool_call in msg["tool_calls"]:
            func_name = tool_call["function"]["name"]
            arguments = json.loads(tool_call["function"]["arguments"])
            print(f"Tool call requested: {func_name}({arguments})")
```

---

## 8. Quick Integration Cheat Sheet

| Task | Configuration |
|---|---|
| **Base URL** | `http://127.0.0.1:8877/v1` |
| **Default Reasoning Model** | `gemini-3.8-flash-high` |
| **Balanced / Fast Model** | `gemini-3.8-flash-medium` or `gemini-3.8-flash` |
| **Pro Reasoning Model** | `gemini-3.1-pro-high` |
| **Images & PDFs** | Base64 data URI in `image_url.url` (`data:image/png;base64,...`, `data:application/pdf;base64,...`) |
| **Streaming** | `"stream": true` with SSE parser |
| **Chat History** | Append user and assistant messages into `messages: []` |
