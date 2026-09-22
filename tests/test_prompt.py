"""Checks on how the prompt sent to the local model is assembled.

Two things matter here, both for speed. The static half of the system prompt
has to be byte-for-byte identical on every request, or Ollama cannot reuse
its cached attention state for it and pays to re-read a couple of thousand
tokens each time. And the parts that grow without bound — saved facts, study
notes, conversation history — have to stay inside their budgets, because on
CPU-only hardware prompt length is felt directly as a wait before the first
word appears.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _layer_shell_stub  # noqa: E402

_layer_shell_stub.install()

import linux_agent_apple as app  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def check_true(label, got):
    if not got:
        failures.append(f"{label}: expected true, got {got!r}")


# -- the static half is genuinely static ------------------------------------
check_true("no leftover doubled braces in the static prompt",
           "{{" not in app.SYSTEM_PROMPT_STATIC and "}}" not in app.SYSTEM_PROMPT_STATIC)
check_true("the tool call shapes survived unescaping",
           '{"tool": "open_url", "url": "https://..."}' in app.SYSTEM_PROMPT_STATIC)
check_true("no unfilled placeholders are left",
           "{today}" not in app.SYSTEM_PROMPT_STATIC
           and "{context_block}" not in app.SYSTEM_PROMPT_STATIC)

# Build the full prompt twice with different volatile state, and confirm the
# static part in front is unchanged.
app.recent_actions.clear()
app.get_active_window_context = lambda: None
first = app._build_system_content()

app.recent_actions.append({"tool": "open_url", "summary": "Opened https://example.com"})
app.get_active_window_context = lambda: "firefox: Some page title"
second = app._build_system_content()

check_true("the two prompts really do differ", first != second)
check_true("both start with the same static prefix",
           first.startswith(app.SYSTEM_PROMPT_STATIC)
           and second.startswith(app.SYSTEM_PROMPT_STATIC))
check_true("the static prefix is a large share of the prompt",
           len(app.SYSTEM_PROMPT_STATIC) > 3000)
check_true("volatile context lands after the static half",
           "firefox: Some page title" in second[len(app.SYSTEM_PROMPT_STATIC):])
check_true("the date is in the volatile half, not the static one",
           "Today's date is" not in app.SYSTEM_PROMPT_STATIC
           and "Today's date is" in second)

# -- unbounded context gets clipped -----------------------------------------
many_facts = "Known facts about the user:\n" + "\n".join(
    f"- [general] fact number {i} about the user" for i in range(200))
clipped = app._clip(many_facts, app.MAX_FACTS_CHARS, "facts")
check_true("a huge fact list is clipped to its budget",
           len(clipped) <= app.MAX_FACTS_CHARS + 60)
check_true("clipping keeps the header",
           clipped.startswith("Known facts about the user:"))
check_true("clipping keeps the newest entries, not the oldest",
           "fact number 199" in clipped and "fact number 0 " not in clipped)
check_true("clipping says how much it dropped", "not shown" in clipped)

short = "Known facts about the user:\n- [general] just the one"
check("something already within budget is left alone",
      app._clip(short, app.MAX_FACTS_CHARS, "facts"), short)
check("empty context stays empty", app._clip("", app.MAX_FACTS_CHARS, "facts"), "")

# -- history is trimmed in both directions ----------------------------------
history = []
for i in range(30):
    history.append({"role": "user", "content": f"message {i} " + "x" * 5000})
    history.append({"role": "assistant", "content": f"reply {i}"})

trimmed = app._trim_history(history)
check("history is capped to the configured number of turns",
      len(trimmed), app.MAX_HISTORY_TURNS)
check_true("the turns kept are the most recent ones",
           "message 29 " in trimmed[-2]["content"])
check_true("an overlong single message is shortened",
           all(len(m["content"]) <= app.MAX_HISTORY_MESSAGE_CHARS + 20 for m in trimmed))
check_true("shortening is marked rather than silent",
           any("trimmed" in m["content"] for m in trimmed))
check_true("roles are preserved",
           {m["role"] for m in trimmed} == {"user", "assistant"})
check("a short history is passed through untouched",
      app._trim_history([{"role": "user", "content": "hi"}]),
      [{"role": "user", "content": "hi"}])

# -- the generation options are sane ----------------------------------------
check_true("the model is asked to stay resident", app.OLLAMA_KEEP_ALIVE)
check_true("output length is capped", app.OLLAMA_OPTIONS.get("num_predict", 0) > 0)
check_true("temperature is low enough for reliable tool-call JSON",
           app.OLLAMA_OPTIONS.get("temperature", 1) <= 0.3)

# -- streaming reply extraction ---------------------------------------------
check("a partial reply is read out of incomplete JSON",
      app.extract_partial_reply('{"actions": [], "reply": "Opening Fire'),
      "Opening Fire")
check("a newline escape is decoded",
      app.extract_partial_reply(r'{"reply": "line one\nline two"}'), "line one\nline two")
check("an escaped quote is decoded",
      app.extract_partial_reply(r'{"reply": "she said \"hi\""}'), 'she said "hi"')
check("an escaped backslash is not mistaken for another escape",
      app.extract_partial_reply(r'{"reply": "C:\\nope"}'), r"C:\nope")
check("a unicode escape is decoded",
      app.extract_partial_reply(r'{"reply": "caf\u00e9"}'), "caf\u00e9")
check("no reply field yet gives nothing",
      app.extract_partial_reply('{"actions": ['), None)

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("prompt assembly checks passed")
