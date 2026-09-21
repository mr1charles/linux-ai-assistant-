# Little Toby

A local-first, open-source desktop AI assistant for Hyprland/Arch-based
Linux. Summon it with a hotkey — a small animated chibi face wakes up
inside a rainbow ring flash, slides up, and reveals a text box. Type
natural language and it controls your desktop (open apps/sites, send
Discord messages, close windows/tabs), answers questions, explains code,
checks your email, reads your screen (OCR) when asked, and — with your
explicit one-time confirmation each session — can move the mouse, type,
click, and install packages. It also keeps a personal knowledge graph and
study notes, and can shift into a visible **Study Mode** on a schedule or
automatically when it recognizes schoolwork on screen.

Everything is parsed by a local LLM through [Ollama](https://ollama.com) —
**fully offline by default.** The only things that ever touch the network
are things that inherently must: sending a Discord message, checking email
over IMAP, or a browser loading a site you asked it to open.

## Three modes, one core

| Script | How you interact | Best for |
|---|---|---|
| `scripts/linux_agent_apple.py` | Little Toby — animated chibi assistant (Hyprland layer-shell) | **Recommended.** Laptops — no wake word needed |
| `scripts/linux_agent_overlay.py` | Plain toggleable GTK text box, no character | A lighter-weight fallback UI |
| `scripts/linux_agent_assistant.py --assistant` | Wake word ("computer") + voice, talks back | Hands-free, desktop use |
| `scripts/linux_agent_offline.py` | Plain CLI, one-shot or interactive typing | Scripting, quick tests |

## Install

```bash
git clone https://github.com/YOUR_USERNAME/linux-agent.git
cd linux-agent
./install.sh
```

This installs Ollama + a local model (`qwen2.5:7b-instruct`, ~4.7GB — chosen
for reliable JSON tool-calling), GTK3 + gtk-layer-shell, `ydotool` (mouse/
keyboard control), `grim` + `tesseract` (screen reading), espeak-ng, the
offline speech model (~50MB), and Python dependencies. It will also offer to
add a `Super+G` keybind and autostart line to your Hyprland config.

Targets Arch/CachyOS (`pacman`); on other distros install the packages
listed at the top of `install.sh` manually, then re-run with
`SKIP_SYSTEM_PACKAGES=1 ./install.sh`.

To remove the keybind/autostart later (without deleting your data), run
`./uninstall.sh`.

## Configure (optional)

```bash
cp .env.example .env   # done automatically by install.sh if missing
```

Only fill in what you actually want:
- `DISCORD_WEBHOOK_URL` — channel Settings → Integrations → Webhooks
- `EMAIL_ADDRESS` / `EMAIL_APP_PASSWORD` — Gmail: enable IMAP in settings,
  then create an [App Password](https://myaccount.google.com/apppasswords)
  (requires 2-Step Verification). Other providers: change `IMAP_SERVER`.
  Gmail users can also use real Gmail search syntax when asking to check
  email (e.g. "check email from:boss after:2026/01/01 has:attachment").
- `ALLOWED_APPS` / `ALLOWED_PACKAGES` — near the top of
  `scripts/linux_agent_apple.py`. `open_app` only launches apps in this
  allowlist by name; `install_package` only offers to install packages in
  this list (empty by default — add ones you trust).

## Run

```bash
set -a; source .env; set +a          # bash/zsh
python3 scripts/linux_agent_apple.py &
```
```fish
source scripts/load-env.fish         # fish
python3 scripts/linux_agent_apple.py &
```

Press **Super+G** to summon Toby (if install.sh added the keybind), or
toggle manually:
```
pkill -SIGUSR1 -f linux_agent_apple.py
```

Or run it as a systemd user service instead of `exec-once`:
```bash
cp systemd/linux-toby.service ~/.config/systemd/user/
systemctl --user enable --now linux-toby
```

## Using Toby

- **Double-click the face** to expand into the full app: a left sidebar with
  four sections —
  - **Dashboard**: live AI status, current task, CPU/memory, context window
    usage, active tool count, running automations (Study Mode / school
    schedule), and messages sent today.
  - **Chat**: your conversation history for the session.
  - **Memories**: an interactive graph of facts it's learned about you —
    drag to pan, scroll to zoom, click a node to see it and delete it, or
    add one by hand. Three layouts, because they answer different
    questions: **Force-directed** shows which parts of what Toby knows are
    densely related, **Radial** gives each category a wedge so their sizes
    are comparable at a glance, and **Mind map** lays categories out as
    bands you can read straight down. Your choice is remembered. "Export as
    Tree/Bubble Image" still renders the old static images if you want a
    picture instead.
  - **Settings**: response style (concise/balanced/detailed), a toggle for
    whether it remembers facts from chat at all, a toggle for Dynamic
    Island notifications, an accent color, and an Ollama model override —
    all saved to `settings.json`.
- **A pill at the top of the screen** (the "Dynamic Island") appears while
  Toby is working in the background — thinking, or reading your screen.
  **Click** it to bring the panel forward; **double-click** it to open a
  task-manager overlay with a live step tracker, elapsed time, and a real
  **Cancel** button. When a reply finishes while the panel is closed it
  becomes a card instead, showing the answer itself with four buttons:
  **Open** brings the panel up with the reply in it, **Explain** asks Toby
  to go into more detail, **Later** brings the same card back in five
  minutes, and **Dismiss** drops it. It waits for you rather than timing
  out.
- **Escape** or the ✕ button closes the panel.
- The first time in a session it needs to move your mouse, type/click, or
  install a package, it shows an inline **Yes/No** confirmation — nothing
  happens until you approve it. Mouse/keyboard access stays granted for the
  rest of the session; package installs ask again every time and always
  open a visible terminal where *you* type your own password.
- "what does my screen say" / "read this" → it screenshots + OCRs the
  screen for that one answer; it never does this silently.
- **Study Mode** shows a floating helper panel (top-right) with three
  buttons: **Summarize Screen** (OCRs the screen once and saves a summary +
  up to 3 difficult-concept explanations as study notes — unless it looks
  like a graded assignment, in which case it deliberately declines to
  summarize/answer it and tells you so instead), **Quiz Me**, and
  **Flashcards** (both built strictly from your saved study notes, never
  invented). Every one of these is a button you click — nothing about
  Study Mode reads your screen or generates content on its own timer in the
  background. That's a deliberate line: silently watching your screen while
  you study and auto-generating "help" is a fast path to an AI quietly
  doing your homework for you, which this project intentionally does not
  do. Toggle Study Mode by asking Toby, describing your school schedule, or
  it can turn on automatically if it recognizes Google Classroom/Kami on
  screen during a check (see `school_mode.json`).
- **Study Plan** (a sidebar tab, expand the panel to reach it) is the
  longer-term version of the above: upload a PDF or text file (a textbook
  chapter, your own notes, an assignment) and Toby extracts the subject and
  specific topics it covers — for a graded assignment it only tracks the
  subject area, never the answers, same rule as Summarize Screen above.
  Every topic shows up as a card on a grid, color-coded by how well you
  know it. That proficiency score **only ever moves when you actually take
  a quiz on that topic** — it's never guessed, self-reported, or inferred
  from just uploading something. Click a card to see your score, quiz
  yourself on it, or get resource links. Those links are plain search
  URLs (YouTube, Khan Academy, Wikipedia) for the topic, not a specific
  video or article the AI picked — Toby has no way to verify a specific
  link actually exists or is any good, so it hands you a real search
  instead of a confident-sounding guess. A "Grade level" setting (5th
  grade through college) adjusts how complex its study explanations are.
- **Voice Mode**: tap the mic button on the main bar to toggle continuous
  voice mode on/off, or **hold it down for push-to-talk** regardless of
  that setting. With wake-word mode on (the default — configurable in
  Settings), say **"toby"** (or whatever `WAKE_WORD` is set to) before your
  request — "hey toby, how much evidence do I need for a text analysis
  essay" works, and the leading "hey" is stripped before the model sees it.
  With wake-word mode off, everything you say is treated as a command. A live
  waveform (genuinely driven by your mic's input level, not decorative) and
  transcript appear while it's listening, and Toby speaks replies back via
  espeak-ng, sentence-by-sentence as they stream in for lower latency —
  queued in order, so each sentence finishes before the next one starts.
  Talking over Toby while it's speaking genuinely interrupts it — it's not
  just muted, the speech process is killed, anything still queued is thrown
  away, and it starts listening for your new request. Voice/rate/pitch/wake-word are all configurable in Settings.
  Worth knowing: Voice Mode does *not* do real acoustic emotion detection —
  "mood" still comes from the LLM reading the text of what you said, same
  as typed chat; building genuine audio-based emotion detection would need
  a whole separate trained classifier this project doesn't have, so it's
  not faked here.
- **Camera Mode**: tap the camera button on the main bar (or the switch in
  Settings) to turn on webcam-based hand and face gestures. Built in:
  open palm = toggle the panel, fist = hide it, swipe left/right = switch
  workspace, push forward/pull back = confirm/cancel, spread fingers =
  toggle fullscreen, rotate your hand = cycle the sidebar section,
  double-blink = confirm, shake your head = cancel, raise your eyebrows =
  expand the sidebar, smile = a little happy pulse from Toby. A small HUD
  in the bottom-left shows a live hand skeleton and the last gesture
  detected. You can also record your own custom gesture in Settings: hold
  a distinct hand pose for about a second while recording, pick which
  action it should trigger, and it's matched by pose similarity from then
  on (not a trained model — it compares your hand's shape to the recorded
  template, so it works best for a clearly distinct static pose rather
  than a motion).
  A few honest scoping notes on this one:
  - **No video is ever displayed, saved, or sent anywhere** — only the
    numeric hand/face landmark positions MediaPipe extracts per frame are
    used, which is also just a lot simpler and faster than compositing
    live video into the UI.
  - Body/posture tracking (lean direction, shoulder position, walking
    toward/away from the camera) from the original design brief is **not**
    implemented — hand and face tracking alone are already a lot of moving
    parts, and running a third full-body model continuously would add
    real CPU cost for a feature I'd rather get right in a focused follow-up
    than bolt on half-working here.
  - "Gaze direction" (used for looking left/right toward things) is a
    coarse iris-position estimate, not proper eye-tracking — good enough
    for a rough left/right signal, not for precisely aiming at UI elements.
  - **Hand pointing** (Settings → Camera Mode → "Point with your hand to
    move the cursor") lets your index fingertip drive the real cursor, with
    a pinch of thumb and index to click, so you can select things without
    reaching for the trackpad. It's off by default, and the first time you
    switch it on Toby asks for the same one-time mouse-control permission
    any other pointer action needs — it does nothing until you say yes.
    Turning Camera Mode off turns it off too. Movement is smoothed and the
    outer edge of the camera's view is ignored, since that's both awkward
    to reach and where tracking is least reliable.
  - Genuinely floating "holographic" menus that follow your hand in 3D
    space aren't implemented. With hand pointing off, a pinch-and-hold
    still drags Toby's panel up and down the screen in real time.
  - `mediapipe` is a hefty, sometimes finicky dependency — if it fails to
    install, `install.sh` still installs everything else and Camera Mode
    just reports itself unavailable instead of breaking the app. Newer
    mediapipe releases also need two separate model files (not just the
    pip package) — `install.sh` downloads them automatically, but if you
    installed mediapipe manually you'll need:
    ```bash
    curl -L https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task -o ~/linux-agent/hand_landmarker.task
    curl -L https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task -o ~/linux-agent/face_landmarker.task
    ```
  - A gesture has to hold steady for a handful of consecutive frames before
    it fires, specifically to keep a single misread frame from randomly
    popping the panel open — if a gesture feels unresponsive, hold the pose
    a beat longer rather than flicking it.
- **Smart mode**: an optional, off-by-default escape hatch to a cloud model
  (OpenAI only, for now) for requests the local model struggles with. Add
  your API key in Settings → Cloud AI and save, then tap the **Smart**
  button on the main bar whenever you want the *next* message(s) to go to
  the cloud instead of local Ollama — it stays on until you tap it again.
  Saving a key is all the setup there is; Smart won't switch on without one
  and says so rather than quietly falling back to the local model. There's
  also a "use cloud automatically for voice requests" switch, for when you
  want hands-free requests to be fast without thinking about it. A few
  things worth knowing:
  - This is entirely opt-in and off by default — nothing leaves your
    machine unless you turn this on and press Smart yourself.
  - Your key is stored only in `settings.json` on your machine (already
    gitignored) and sent only to `api.openai.com` — never to Anthropic, a
    telemetry endpoint, or anywhere else.
  - **Never paste a real API key into a chat conversation with an AI
    assistant (including this one) to ask it to be added to a config** —
    treat any key that touches a chat transcript as compromised and
    rotate it. Type it directly into Toby's own Settings field instead.
  - There's no usage tracking or spending cap here — you're billed by
    OpenAI directly for whatever Smart mode is used for, so treat the
    toggle deliberately rather than leaving it on by default.
- "remember I like X" / genuine facts about you → saved to a local
  knowledge graph, shown back to you via the sidebar buttons, never
  fabricated.
- "add this to my [subject] notes: ..." or working through material in
  conversation → saved as study notes, usable later for quizzes/summaries/
  flashcards from your real material only.
- "study mode on/off" or describing your school schedule ("I have school
  Mon–Fri 7:30 to 3") → toggles a visible Study Mode badge, either by hand
  or automatically on that schedule / when it recognizes Google Classroom
  or Kami on screen (edit `adaptive_keywords` in `school_mode.json`, or ask
  Toby to set your schedule).

## What it can do

- "open youtube then tiktok" — opens sites in order
- "message discord saying I'm live" — posts via your webhook
- "check my email from bob" / "any emails about the invoice" — pulls
  matching subject/sender lines, or a full Gmail search query
- "close this tab" vs "close this window" — Ctrl+W vs killing the app
- Coding questions — shown as text/code in the reply, it does not write
  files
- General chat/questions — plain conversation, remembers the session and
  persists history/knowledge/notes across restarts

## Speed

Most of the wait before Toby says anything is the local model reading its
own prompt, so that is where the effort went:

- The system prompt is split in two. The large half holding the tool list
  and the rules is byte-for-byte identical every time, so Ollama serves it
  from its cached attention state instead of reprocessing it. Everything
  that changes — the date, the focused window, saved facts and notes, your
  settings — is appended after it. **If you add context of your own, put it
  in the tail block**: anything volatile in the middle of the static half
  throws the whole cached prefix away.
- Saved facts and study notes are clipped to a character budget rather than
  pasted in whole. They accumulate forever, and left unbounded they were
  several times the size of the prompt itself.
- The model is asked to stay resident between requests and warmed up at
  startup, so the first thing you say after a break doesn't pay to load
  several gigabytes off disk.

If it's still slow, the two real levers are a lighter model (below) and
Smart mode, which sends the request to a cloud model instead.

## Notes

- `qwen2.5:7b-instruct` needs a decent GPU or ~8GB RAM for reasonable
  latency. On a lighter laptop, `ollama pull qwen2.5:1.5b` and set
  `OLLAMA_MODEL=qwen2.5:1.5b` in `.env` — it's less reliable at strict JSON
  formatting, so tool-calling may misfire more often. A model set in
  Settings applies everywhere, including fact extraction, quizzes,
  flashcards and screen summaries.
- Mouse and keyboard control need `ydotoold` running:
  `systemctl --user enable --now ydotool.service`. Without it every
  ydotool call hangs until it times out. Toby copes with both the current
  ydotool argument syntax and the pre-1.0 one, and tells you which of the
  two failed rather than reporting success for something that didn't
  happen.
- `espeak-ng` sounds robotic (voice mode only). For nicer offline TTS, look
  at `piper-tts` — heavier model but much more natural.
- The layer-shell positioning was written for Hyprland; adjust margins in
  `linux_agent_apple.py` if it doesn't land right on your screen or
  compositor.
- Everything Toby learns lives in plain JSON/Markdown files under
  `~/linux-agent/` (`knowledge.json`, `study_notes.json`, `history.json`,
  `session_log.md`, `school_mode.json`, `settings.json`) — delete any of them to reset that
  part, or back them up like any other file.

## Working on it

```bash
./tests/run.sh
```

Toby's windows are all layer-shell surfaces, so the app itself only really
runs on a Wayland compositor. These checks cover the part that doesn't need
one, which is most of the code, and they run anywhere GTK3 and Xvfb are
installed:

- every script byte-compiles
- the speech queue, against a stand-in for espeak-ng
- the ydotool key and click translation, against a stand-in ydotool, in
  both argument syntaxes and with none installed
- each Memories graph layout: nodes inside the view, spread out rather than
  stacked, and the same picture twice running
- the stylesheet, through the real GTK CSS parser, for ten accent colours
  including malformed ones, plus a check that every class the code applies
  exists in the sheet
- the prompt: that its static half really is static, and that the context
  budgets and history trimming hold
- the whole widget tree, constructed against real GTK with GtkLayerShell
  stubbed out, every custom Cairo widget drawn, and hand pointing driven
  through the real camera callback

What they can't cover is anchoring, margins, input regions and anything
else that only means something to a live compositor — those still need a
look on the real machine.

`tests/run.sh` points `HOME` at a throwaway directory seeded with sample
data, so it never reads or writes your real notes, history or settings.

## License

MIT
