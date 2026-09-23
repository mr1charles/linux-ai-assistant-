"""Which local model gets used, given what's installed."""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import model_picker as mp  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


check("a model chosen in Settings always wins",
      mp.pick("llama3.2:3b", "qwen2.5:7b-instruct", ["qwen3:4b-instruct-2507-q4_K_M"]), "llama3.2:3b")
check("then one set in .env",
      mp.pick("", "qwen2.5:7b-instruct", ["qwen3:4b-instruct-2507-q4_K_M"]), "qwen2.5:7b-instruct")
check("otherwise the best installed",
      mp.pick("", "", ["qwen2.5:7b-instruct:latest", "qwen3:4b-instruct-2507-q4_K_M"]),
      "qwen3:4b-instruct-2507-q4_K_M")
check("someone with only the old model keeps it",
      mp.pick("", "", ["qwen2.5:7b-instruct"]), "qwen2.5:7b-instruct")
check("a bare name matches its :latest tag",
      mp.pick("", "", ["qwen3:4b-instruct:latest"]), "qwen3:4b-instruct")
check("nothing installed (or Ollama down) falls back to the old default",
      mp.pick("", "", []), mp.FALLBACK)
check("an unrelated model isn't mistaken for a preferred one",
      mp.pick("", "", ["qwen3:4b-thinking-2507"]), mp.FALLBACK)

check("original Qwen3 is told not to think", mp.is_hybrid_thinker("qwen3:4b"), True)
check("the 2507 instruct build isn't", mp.is_hybrid_thinker("qwen3:4b-instruct-2507-q4_K_M"), False)
check("a thinking build is left alone", mp.is_hybrid_thinker("qwen3:4b-thinking-2507"), False)
check("older families are left alone", mp.is_hybrid_thinker("qwen2.5:7b-instruct"), False)
check("an unreachable Ollama lists nothing",
      mp.installed_models("http://127.0.0.1:9/api/chat", timeout=0.5), [])

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("model picker checks passed")
