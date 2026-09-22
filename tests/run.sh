#!/usr/bin/env bash
# Everything that can be checked without a Wayland compositor.
#
# Little Toby's real home is Hyprland, and its windows are all layer-shell
# surfaces, so the app itself can only really be exercised there. These
# checks cover the part that doesn't need a compositor — which is most of
# the code — and they run anywhere GTK3 and Xvfb are installed.
set -u
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
status=0

# A throwaway HOME so nothing here reads or writes the real history,
# knowledge or settings files. It is seeded with a little data, because
# several code paths (the memory graph in particular) do nothing at all when
# there is none, and a check that exercises nothing proves nothing.
make_home() {
    local home
    home="$(mktemp -d)"
    mkdir -p "$home/linux-agent"
    cat > "$home/linux-agent/knowledge.json" <<'JSON'
{"facts": [
  {"id": "f1", "text": "Takes English, Biology and Algebra 2", "category": "school", "added": "2026-01-05T09:00:00"},
  {"id": "f2", "text": "Writes text analysis essays for English", "category": "school", "added": "2026-01-06T09:00:00"},
  {"id": "f3", "text": "Prefers short answers", "category": "preferences", "added": "2026-01-07T09:00:00"}
]}
JSON
    cat > "$home/linux-agent/study_notes.json" <<'JSON'
{"notes": [
  {"id": "n1", "subject": "English", "topic": "Text analysis essays",
   "content": "Each claim needs evidence quoted from the text, then explained.",
   "added": "2026-01-08T09:00:00"}
]}
JSON
    cat > "$home/linux-agent/study_plan.json" <<'JSON'
{"topics": [
  {"id": "t1", "subject": "English", "topic": "Text analysis essays",
   "proficiency": null, "quiz_count": 0, "last_assessed": null, "sources": []},
  {"id": "t2", "subject": "Biology", "topic": "Cell respiration",
   "proficiency": 41, "quiz_count": 2, "last_assessed": null, "sources": []},
  {"id": "t3", "subject": "Algebra 2", "topic": "Quadratics",
   "proficiency": 88, "quiz_count": 4, "last_assessed": null, "sources": []}
]}
JSON
    echo "$home"
}

echo "== byte-compiling every script =="
"$PY" -m py_compile scripts/*.py || status=1

echo "== voice engine checks =="
"$PY" tests/test_voice_engine.py || status=1

echo "== screen control checks =="
"$PY" tests/test_screen_control.py || status=1

echo "== memory graph layout checks =="
"$PY" tests/test_knowledge_graph.py || status=1

echo "== fast path checks =="
"$PY" tests/test_fast_path.py || status=1

echo "== model picker checks =="
"$PY" tests/test_model_picker.py || status=1

echo "== desktop event listener checks =="
timeout 60 "$PY" tests/test_hypr_events.py || status=1

echo "== lid fold state machine checks =="
"$PY" tests/test_fold.py || status=1

echo "== chibi motion and drawing checks =="
"$PY" tests/test_chibi.py || status=1

echo "== GTK CSS parse check =="
if command -v xvfb-run >/dev/null 2>&1; then
    xvfb-run -a "$PY" tests/css_check.py || status=1
else
    echo "   skipped: xvfb-run not installed"
fi

echo "== prompt assembly checks =="
if command -v xvfb-run >/dev/null 2>&1; then
    tmp_home="$(make_home)"
    HOME="$tmp_home" xvfb-run -a "$PY" tests/test_prompt.py || status=1
    rm -rf "$tmp_home"
else
    echo "   skipped: xvfb-run not installed"
fi

echo "== headless UI smoke test =="
if command -v xvfb-run >/dev/null 2>&1; then
    tmp_home="$(make_home)"
    HOME="$tmp_home" xvfb-run -a "$PY" tests/smoke_headless.py || status=1
    rm -rf "$tmp_home"
else
    echo "   skipped: xvfb-run not installed"
fi

echo "== fold overlay window =="
if command -v xvfb-run >/dev/null 2>&1; then
    xvfb-run -a "$PY" tests/smoke_fold_overlay.py || status=1
else
    echo "   skipped: xvfb-run not installed"
fi

echo "== a real task with the chibi =="
if command -v xvfb-run >/dev/null 2>&1; then
    tmp_home="$(make_home)"
    HOME="$tmp_home" xvfb-run -a -s "-screen 0 1280x1024x24" "$PY" tests/smoke_chibi_task.py || status=1
    rm -rf "$tmp_home"
else
    echo "   skipped: xvfb-run not installed"
fi

if [ "$status" -eq 0 ]; then
    echo "== all checks passed =="
else
    echo "== FAILURES above =="
fi
exit "$status"
