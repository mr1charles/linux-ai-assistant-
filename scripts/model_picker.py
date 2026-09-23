"""
model_picker.py — use the best local model you actually have installed.

The model used to be hardcoded as qwen2.5:7b-instruct. The newer Qwen3 4B
instruct release is a better fit for this laptop on both counts that matter:
it is smaller, so each word comes out faster on a CPU, and it follows
instructions and produces tool-call JSON more reliably than the older 7B.

But a hardcoded new name would break anyone who hasn't downloaded it, so the
choice is made at startup from what Ollama reports as installed:

1. a model you picked in Settings, always;
2. otherwise OLLAMA_MODEL from your .env, if you set one;
3. otherwise the first of PREFERRED that is installed;
4. otherwise the old default, so nothing changes for anyone who has only
   that — the first request then says plainly if it's missing.

The installer downloads the first preferred model it can, so a fresh install
lands on the fast one.
"""

import requests

# What the installer downloads is qwen3:4b. The instruct-only builds are
# listed first in case you've pulled one yourself; they skip "thinking".
PREFERRED = [
    "qwen3:4b-instruct-2507-q4_K_M",
    "qwen3:4b-instruct",
    "qwen3:4b",
    "qwen2.5:7b-instruct",
    "qwen2.5:3b-instruct",
    "llama3.2:3b",
]
FALLBACK = "qwen2.5:7b-instruct"


def installed_models(chat_url, timeout=2.0):
    """Names Ollama has locally, or [] if it can't be reached."""
    tags_url = chat_url.replace("/api/chat", "/api/tags")
    try:
        resp = requests.get(tags_url, timeout=timeout)
        resp.raise_for_status()
        return [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception:
        return []


def _same(a, b):
    """'qwen3:4b' matches 'qwen3:4b' and 'qwen3:4b-...'? No — only exact,
    except that a bare name matches its ':latest'."""
    a, b = a.strip(), b.strip()
    return a == b or a + ":latest" == b or b + ":latest" == a


def pick(settings_model="", env_model="", installed=()):
    if settings_model:
        return settings_model
    if env_model:
        return env_model
    for candidate in PREFERRED:
        if any(_same(candidate, name) for name in installed):
            return candidate
    return FALLBACK


def is_hybrid_thinker(model):
    """Models that "think" out loud before answering unless told not to.

    The original Qwen3 releases reason at length by default — useful for
    maths, ruinous for a voice assistant's latency. The 2507 instruct build
    and older families don't, and are sent nothing extra.
    """
    name = model.lower()
    if not name.startswith("qwen3"):
        return False
    return "instruct" not in name and "thinking" not in name
