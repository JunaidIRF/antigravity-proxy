#!/usr/bin/env python3
"""Interactive test script to send an image + text prompt to the local Antigravity Proxy.

Works with PNG, JPG, JPEG, WEBP, GIF, and PDF documents.
Pure Python standard library — no pip dependencies required.
"""

import base64
import json
import mimetypes
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROXY_URL = "http://127.0.0.1:8877/v1/chat/completions"
DEFAULT_MODEL = "gemini-3.8-flash-high"


def clean_path(raw_input: str) -> str:
    """Clean up Windows drag-and-drop file paths with quotes."""
    p = raw_input.strip()
    if (p.startswith('"') and p.endswith('"')) or (p.startswith("'") and p.endswith("'")):
        p = p[1:-1].strip()
    if p.startswith("& "):
        p = p[2:].strip().strip('"\'')
    return os.path.abspath(os.path.expanduser(p))


def get_mime_type(file_path: str) -> str:
    """Detect MIME type from file extension."""
    mime, _ = mimetypes.guess_type(file_path)
    if mime:
        return mime
    ext = os.path.splitext(file_path)[1].lower()
    mapping = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".pdf": "application/pdf",
    }
    return mapping.get(ext, "image/png")


def main():
    print("=" * 60)
    print("   Antigravity Proxy — Image & Multimodal Chat Tester")
    print("=" * 60)
    print(f"Target Proxy: {PROXY_URL}\n")

    # 1. Get Image Path
    while True:
        raw_path = input("Enter image or PDF path (or drag & drop here): ").strip()
        if not raw_path:
            print("Path cannot be empty. Please enter a valid path.\n")
            continue
        image_path = clean_path(raw_path)
        if not os.path.isfile(image_path):
            print(f"File not found: {image_path}\nPlease check the path and try again.\n")
            continue
        break

    # 2. Read and Base64-encode
    mime_type = get_mime_type(image_path)
    file_size_kb = os.path.getsize(image_path) / 1024
    print(f"\n[OK] Loaded: {os.path.basename(image_path)} ({file_size_kb:.1f} KB, MIME: {mime_type})")

    try:
        with open(image_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        print(f"[Error] Failed to read file: {e}")
        return

    data_uri = f"data:{mime_type};base64,{b64_data}"

    # 3. Get User Prompt
    default_prompt = "Describe this image in detail and highlight anything interesting."
    if mime_type == "application/pdf":
        default_prompt = "Summarize this PDF document and list its key points."

    print(f"\nDefault prompt: \"{default_prompt}\"")
    prompt = input("Enter your prompt (press Enter to use default): ").strip()
    if not prompt:
        prompt = default_prompt

    # 4. Model Selection
    model = input(f"\nEnter model [default: {DEFAULT_MODEL}]: ").strip()
    if not model:
        model = DEFAULT_MODEL

    # 5. Build OpenAI-compatible Request Payload
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

    print(f"\nSending request to {model} via proxy...")
    req = Request(
        PROXY_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        print("\n" + "=" * 60)
        print("Model Response:")
        print("=" * 60)
        message = data["choices"][0]["message"]["content"]
        print(message)
        print("=" * 60)

        usage = data.get("usage", {})
        if usage:
            print(f"Tokens: Prompt={usage.get('prompt_tokens', 0)} | Completion={usage.get('completion_tokens', 0)} | Total={usage.get('total_tokens', 0)}")

    except HTTPError as e:
        err_msg = ""
        try:
            err_msg = e.read().decode("utf-8")
        except Exception:
            pass
        print(f"\n[HTTP Error {e.code}]: {err_msg or e.reason}")
    except URLError as e:
        print(f"\n[Connection Error]: Could not reach proxy at {PROXY_URL}.")
        print("Make sure the proxy is running! (Double-click `start_proxy.bat`)")
    except Exception as e:
        print(f"\n[Error]: {e}")


if __name__ == "__main__":
    main()
