# Little Toby

A local-first desktop AI assistant for Hyprland on Arch/CachyOS, with a
character. Press Super+G and a small chibi face wakes up in a rainbow ring
at the bottom of your screen. Ask it something and it answers; ask it to
*do* something and the face pops out of its pill, grows a little body, and
walks across your screen doing it — the pointer carried in its hand, typing
with its hands on an unseen keyboard, a checklist beside it crossing off
each step as it's done. When it's finished it cheers and hops back home.

It opens apps and sites, closes windows and tabs, sends Discord messages,
checks email, reads your screen when asked, keeps a personal knowledge
graph and study notes, quizzes you, and shifts into Study Mode on a
schedule. With your one-time permission each session — by button or by
fingerprint — it can move the mouse, type and click. You can also talk to
it from your phone, from anywhere, and watch it work.

Everything runs on a local model through [Ollama](https://ollama.com) —
**fully offline by default.** The only things that touch the network are
things that inherently must: a Discord message, an email check, a site you
asked it to open, or your own phone reaching your own laptop over your own
private Tailscale network.

When the laptop goes to sleep, the desktop folds shut toward the hinge like
a foldable phone; when you open it again, it unfolds.

## Three modes, one core

| Script | How you interact | Best for |
|---|---|---|
| `scripts/linux_agent_apple.py` | Little Toby — animated chibi assistant (Hyprland layer-shell) | **Recommended.** Laptops — no wake word needed |
| `scripts/linux_agent_overlay.py` | Plain toggleable GTK text box, no character | A lighter-weight fallback UI |
| `scripts/linux_agent_assistant.py --assistant` | Wake word ("computer") + voice, talks back | Hands-free, desktop use |
| `scripts/linux_agent_offline.py` | Plain CLI, one-shot or interactive typing | Scripting, quick tests |

## Install

One command, one password prompt:

```bash
curl -fsSL https://raw.githubusercontent.com/mr1charles/linux-ai-assistant-/main/install.sh | bash
```

or from a checkout, `./install.sh`. Add `--phone` to also install Tailscale
for the phone app.

That installs the system packages with pacman, gives Toby a private Python
environment (it never touches your system Python), downloads the fastest
suitable local model and the offline speech model, and installs two user
services that start at login — Toby itself, and the lid fold. Then it
starts them. Press **Super+G**.

It does **not** edit your Hyprland config. Super+G is added to the running
compositor by Toby when it starts, and only if that key is free (if it
isn't, `toby doctor` tells you and you can pick another in settings.json).
That works the same whether your config is `hyprland.conf` or a Lua setup
like Caelestia's, and a Hyprland reload simply removes it until Toby next
starts.

On other distros, install the packages listed in `install.sh` yourself and
run it with `SKIP_SYSTEM_PACKAGES=1`.

## The `toby` command

Everything after installing is one command:

| | |
|---|---|
| `toby` | summon Toby (same as Super+G) |
| `toby start`, `stop`, `restart`, `status` | the services |
| `toby doctor` | check everything, change nothing |
| `toby sleep` | play the whole lid fold, then suspend |
| `toby fold preview` | play the fold and unfold without suspending |
| `toby phone on`, `pair`, `reset`, `off` | the phone app |
| `toby animations on`, `off` | Toby-style window animations |
| `toby model`, `toby model <name>` | see or choose the local model |
| `toby update` | pull the latest version and refresh dependencies |
| `toby uninstall` | remove services and the command; keeps your data |

`toby doctor` looks at Python packages, programs, services, the model, the
summon key and — for the lid fold — how logind handles the lid, how long it
lets programs delay sleep, whether the fold daemon holds its delay lock,
and whether your Hyprland config also acts on the lid. It explains
anything wrong in a sentence and never changes anything itself.

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

## Running it by hand

The services do this for you. To run Toby in a terminal instead, for
example to watch its output:

```bash
toby stop
./scripts/toby-session.sh linux_agent_apple.py
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

## Toby at work: the chibi

When a request involves the screen — opening something, moving the
pointer, clicking, typing, closing a tab — Toby's face leaves the pill,
grows a body, and does it in front of you:

- It walks to each place it's going to act. The real pointer only moves once
  Toby's hand is there, so the cursor is carried rather than drifting across
  the screen on its own.
- Typing gets a typing pose, clicks a little tap, opening something a reach.
- A speech bubble names the step it's on, and a checklist in the corner
  shows the whole task: finished steps crossed out, the current one in bold,
  the rest waiting. The Dynamic Island shows the same progress —
  "2 of 4 · Click" with a filling ring — even with the panel closed.
- At the end it says the reply, cheers, folds its body away and flies back
  into the pill.

The chibi is on a click-through layer: it never blocks anything underneath
it, and it only draws while it's out. It can be turned off in Settings,
Animations; the task still runs, just without the show.

## The lid fold

When the laptop suspends — lid, menu, or `toby sleep` — the desktop
compresses slightly toward the centre, leans toward you, folds down onto
the hinge with motion blur, shading and a glint along its top edge, and
fades to black. When the laptop wakes, the same motion runs in reverse from
black: it unfolds from the hinge, expands and flattens back into your
desktop.

It can't get between your laptop and sleep. It runs as its own small
service and holds a systemd-logind *delay* lock: logind waits for it, but
never longer than logind's own cap, and never at all if the service has
crashed, because the lock is a file descriptor that closes with the
process. It also lets go on its own deadline whether or not a frame was
drawn, and if it can't take a screenshot there's no animation and no delay.
If a suspend it folded for never comes, it unfolds by itself. None of your
lid or suspend configuration is touched.

One honest limit: a laptop reports the lid closing only when it's almost
shut, so you'll mostly see the first part of the closing fold, if that.
The unfold on opening is fully visible. To watch the whole close, run
`toby sleep` (or bind it to a key): it plays the full fold with the lid
open, then suspends. `toby fold preview` plays both halves without
suspending.

## Talking to Toby from your phone

The phone app is a small installable web app, the same on iPhone and
Android. Turn it on and pair:

```bash
toby phone on
```

That prints a QR code. Scan it with the phone's camera, then **Share, Add
to Home Screen** (iPhone) or **Install app** (Android), and Toby is an app
on your home screen. Type or tap the microphone and say what you want; the
laptop does it, and the phone shows the same live checklist, the reply, and
any permission prompt — which you can answer from the phone.

It works from anywhere your phone has internet, through
[Tailscale](https://tailscale.com): a free, private network between your own
devices. Install it on the laptop (`./install.sh --phone`, or
`sudo pacman -S tailscale`) and on the phone, and sign in to the same
account on both. Toby itself only ever listens on the laptop's loopback
address; Tailscale publishes it to your devices alone, with a real HTTPS
certificate. Nothing is exposed to the public internet and nothing passes
through anyone else's server.

On top of that, every request needs the pairing code from the QR — 256
random bits, kept in a file only you can read, compared in constant time,
with a lockout after repeated wrong guesses. A paired phone can do no more
than you can at the keyboard: the mouse, keyboard and installs still stop
at the same permission prompt. `toby phone reset` unpairs every phone at
once; `toby phone off` turns the whole thing off.

It's a web app rather than a store app on purpose: one version works on
both platforms, updates itself from your laptop, and needs no developer
account or App Store review.

## Fingerprint approval

In Settings, **Approve permission prompts with your fingerprint** lets a
touch of the reader answer the one-time "Let Toby use your mouse and
keyboard?" question. It goes through fprintd, so Toby only ever learns
"matched" or "didn't". A miss never counts as "no"; the buttons always
still work. Enrol a finger first with `fprintd-enroll`.

## Animations

Everything moves on the same small set of curves — quick to start, soft to
settle, no cartoon bounce — so the pill, the chibi, the island and the fold
feel like one thing. The face breathes, blinks, glances toward a workspace
you switch to, looks up when a window opens, squashes when you tap it, hops
when a task is done, and has drifting motes instead of a spinner while it
thinks. While a reply is on its way, the word "Thinking" breathes gently.

Every panel (the pill, the sidebar, the Dynamic Island and its task view,
the Study Helper, quizzes and topic cards) fades and settles into place
through one shared animation engine. Things arrive quickly and leave
faster. If you change your mind halfway, say by summoning the pill while
it's still sinking, it turns around from where it is instead of jumping.
Hover and press feedback, Hyprland's window animations and the phone app
all use the same four curves and five durations.

While a window is fullscreen (a video or a game), Toby holds back anything
you didn't ask for: reply cards wait until you leave fullscreen, and the
login greeting is skipped.

**Settings, Animations, Reduce motion** makes everything appear and
disappear in place, with no travel. It's also on automatically if animations
are turned off system-wide in GTK (`gtk-enable-animations`).

**Settings, Animations** also has a switch for each animation and sliders for
fold speed, strength, perspective, motion blur and shrink, idle
liveliness, tap reaction and walking speed, plus Preview the fold. They're
saved in `settings.json` under `"animations"`, where out-of-range values are
clamped rather than trusted.

`toby animations on` also gives Hyprland's own window, workspace, minimise
(special workspace) and menu animations Toby's curves. That's applied to
the running compositor only, checked by reading back what Hyprland reports,
and undone by `toby animations off` or any Hyprland reload — your config
file is never edited, so Caelestia or Noctalia stay exactly as they were.

It's all tuned for an integrated-graphics laptop. With the pill open and
idle Toby uses a few percent of one core (it was about 18% before this
work), under 1% hidden, and nothing is drawn by any overlay once its
animation ends.

[docs/ANIMATION.md](docs/ANIMATION.md) lists every animation, where it
lives, the shared curves and durations, and how to add a new one.

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

- Obvious one-step requests don't wait for the model at all: "open youtube
  and tiktok", "close this tab", "study mode on", "what time is it" happen
  instantly. Only a request that matches a known shape completely is
  handled this way; anything needing judgement still goes to the model.
- Toby picks the best local model you have installed, preferring the Qwen3
  4B instruct release: smaller than the old 7B default, so faster on a CPU,
  and better at following instructions. `toby model` shows or changes it.

If it's still slow, the other lever is Smart mode, which sends a request to
a cloud model instead.

## Notes

- A model chosen with `toby model` or in Settings applies everywhere,
  including fact extraction, quizzes, flashcards and screen summaries.
  Very small models (1.5B) are faster still but less reliable at producing
  the structured output tool-calling depends on.
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
  `session_log.md`, `school_mode.json`, `study_plan.json`, `settings.json`,
  and `remote.json` for the phone pairing code) — delete any of them to
  reset that part, or back them up like any other file.

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
- the lid fold's state machine through rapid close/open cycles, early
  suspends, crashes mid-draw and failed screenshots — above all, that the
  sleep lock is always released in time — and its window, built for real
- the chibi's motion and drawing, and that no wait on it can hang a task
- a real four-step task through the app with the chibi out, checking order,
  the pointer waiting for the hand, and everything put away afterwards
- a phone request over HTTP, through the app, to the reply coming back; the
  bridge's authentication and lockout; and the phone app itself in headless
  Chromium at phone size
- fingerprint approval against stand-ins for fprintd, including cancel and
  timeout
- the fast path, both what it takes and near-misses it must decline
- model selection, the desktop event listener (including a compositor
  restart), and the runtime Hyprland animations
- the stylesheet, through the real GTK CSS parser, for ten accent colours
- the prompt: that its static half really is static, and the context budgets
- the whole widget tree, constructed against real GTK with GtkLayerShell
  stubbed out, every custom Cairo widget drawn, GTK's own warnings treated
  as failures
- idle CPU use, so a regression in the animation budget is caught

What they can't cover is anchoring, margins, input regions and anything
else that only means something to a live compositor — those still need a
look on the real machine.

`tests/run.sh` points `HOME` at a throwaway directory seeded with sample
data, so it never reads or writes your real notes, history or settings.

## License

MIT
