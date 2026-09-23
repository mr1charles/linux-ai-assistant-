# Little Toby on your phone

Toby lives on your computer. Your phone is its remote: from anywhere, you
tell Toby what to do and watch it happen on the computer, answer its
questions, and see what it found.

```
iPhone app / web app
        │  HTTPS, over your own Tailscale network (WireGuard, end to end)
        ▼
tailscale serve on the computer ──► Toby's phone bridge (127.0.0.1 only)
                                          │
                                          ▼
                     Toby: the model, permission levels, tools, jobs
                                          │
                                          ▼
                  apps · files · browser · terminal · desktop · pen
```

## Setting up

On the computer, once:

```bash
toby phone on
```

That turns on the phone bridge, publishes it to your tailnet with
`tailscale serve`, and starts pairing. You need
[Tailscale](https://tailscale.com) on the computer (`./install.sh --phone`
or `sudo pacman -S tailscale && sudo tailscale up`) and on the phone,
signed in to the same account. Tailscale is what lets the phone reach the
computer from any network without opening anything to the internet: it's a
private WireGuard network between your own devices, relayed (still
encrypted end to end) when they can't connect directly.

### Pairing a phone

1. On the computer: Toby's **Settings → Phone → Connect a phone**, or
   `toby phone pair`. You get a QR code and an eight-character code that
   works once, for five minutes.
2. On the iPhone: open Little Toby and tap **Pair a computer**, then scan
   the code (or type the address and code). On Android or any browser,
   scan with the camera to open the web app.
3. The phone and the computer both show a six-digit number. The iPhone app
   works its number out itself and stops if the computer's doesn't match.
4. Approve on the computer. Only the computer can approve a new phone.

The phone then holds its own 256-bit token (in the iPhone's Keychain); the
computer keeps only a SHA-256 of it.

### Managing phones

- On the computer: Settings → Phone (Unpair buttons), `toby phone devices`,
  `toby phone revoke <name>`, `toby phone reset` (unpairs every phone),
  `toby phone off`.
- On a phone: Settings → Paired phones lets you unpair a lost phone from
  another one; **Log out** unpairs this phone properly.

## What the phone can do

| Screen | What's there |
|---|---|
| Home | Toby, reacting to what the computer is doing; the connection, with a plain explanation of anything wrong; any question waiting for you; the live task with Pause and Stop; the computer's real CPU, memory, battery and uptime; quick actions (customizable) |
| Chat | Type or talk (tap the microphone, or Toby's face on Home). Speech is recognised on the phone where it can be |
| Tasks | Everything you've asked, with each step and what it found, the reply, files it touched, and command output if you want to see it |
| Computer | Work Mode on or off; Overview (open windows, tap to switch), Screen and Toby View (live, if allowed on the computer), Terminal (commands Toby ran), Files |
| Settings | Computers, paired phones, notifications, talking, appearance, quick actions, and what Toby asks about |

Things you can say: "is my game still running?", "check if my project
builds", "run the tests in ~/project and tell me if they pass", "open
Claude Code in my project", "start building the app and let me know when
it's done", "how's the build going?", "put the PDF from Downloads in
Documents", "delete the old zips in Downloads", "what's on my screen?".

Long jobs keep running after the task that started them ends, and Toby
remembers recent tasks, so "continue what I was doing earlier" and "how's
the build going?" work later, from anywhere.

## Permission levels

Every action is one of:

- **Safe**: looking (status, windows, programs, listing, searching and
  reading files, reading the screen), opening apps and pages. Done at once.
- **Asks first**: writing, moving or deleting files (deleting means the
  trash; overwritten files are backed up first), running commands,
  stopping programs, downloading, sending messages, and using the mouse,
  keyboard or pen. Toby asks on the computer and every paired phone at
  once; the first answer counts; ten minutes with no answer is a no.
- **Restricted**: sudo, recursive deletes, disks and partitions, piping
  the internet into a shell, force-pushing, anything outside your home
  folder, your keys and passwords. Refused unless you turn on **Allow
  restricted actions** on the computer, and even then Toby asks each time,
  with the risk spelled out; the iPhone asks you to confirm twice.

## Notifications

The computer records events (a task you started from the phone finished or
failed, Toby needs your OK, a watched build or download finished, the
computer is going to sleep, a phone was paired). Duplicates within a minute
are dropped and there's an hourly cap.

- **While the app is open**, they appear at once as a banner.
- **In the background**, iOS lets the app check in only now and then
  (Background App Refresh), so a notification can be late.
- **For instant delivery**, set `"notify_ntfy_url"` in settings.json to an
  [ntfy](https://ntfy.sh) topic only you know (for example
  `https://ntfy.sh/toby-4f9c2a7e1b`) and subscribe to it in the ntfy app.
  Only the title and one short line are sent. You can run your own ntfy
  server instead.
- Apple push notifications straight to the Little Toby app would need a
  paid Apple Developer account and a push key on the computer; that isn't
  set up.

## The iPhone app: getting it onto your phone

The app is in `ios/`: SwiftUI, iOS 17 or later, generated with
[XcodeGen](https://github.com/yonaskolb/XcodeGen) from `ios/project.yml`.

Every push that changes it is built on a GitHub macOS machine (the
"iPhone app" workflow, `.github/workflows/ios.yml`), which:

1. compiles it for iPhone and packages **LittleToby-unsigned.ipa**;
2. runs the unit tests, and a UI test that pairs, asks, approves and
   finishes a task against a real Toby bridge, on a small and a large
   iPhone, in light and dark, saving screenshots.

Each release on the repository's **Releases** page has it attached as
`LittleToby-<version>-unsigned.ipa`. For a build between releases, take it
from that run's **Artifacts** (Actions tab).

**It's unsigned**, because signing needs an Apple account, and iPhones only
install signed apps. You sign it with your own Apple ID when you install it:

- **From Linux** (no Mac needed): a sideloading tool that signs with your
  Apple ID, such as [Plume Impactor](https://github.com/khcrysalis/Impactor)
  (Linux, macOS and Windows) or AltServer-Linux with AltStore.
- **From Windows or macOS**: [Sideloadly](https://sideloadly.io) or
  [AltStore](https://altstore.io).

With a free Apple ID, a sideloaded app must be re-signed every 7 days (the
tools can do it automatically) and you can have 3 at once. With a paid
Apple Developer account ($99/year) it lasts a year, and you could use
TestFlight instead. Once it's installed, turn on **Settings → Privacy &
Security → Developer Mode** (the phone restarts; the switch only appears
after a sideloaded app is on the phone), then trust your Apple ID under
**Settings → General → VPN & Device Management**.

**On a Mac**, you can also build and run it yourself:

```bash
brew install xcodegen
cd ios && xcodegen generate && open LittleToby.xcodeproj
```

then choose your team under Signing & Capabilities and run it on your phone.

## Security, in one place

- Toby's bridge listens on 127.0.0.1 only. Nothing is exposed to the public
  internet; Tailscale publishes it to your devices alone, over WireGuard,
  with HTTPS on top.
- Pairing needs a one-time code from the computer's screen plus your
  approval on the computer; a comparison number guards against the wrong
  device claiming it.
- Each phone has its own token (256 random bits, stored hashed), compared
  in constant time; wrong tokens or codes lock an address out for a minute.
- Every action goes through the permission levels above; a phone gets no
  more power than you have at the keyboard.
- Screen view is off unless you turn it on at the computer, and the
  computer shows when a phone is looking.
- Requests are size-limited, responses aren't cached, and the web app is
  served with a strict content security policy.
- The computer's own admin routes (used by `toby phone pair`) need a key
  from a file only you can read, and refuse anything that came through the
  Tailscale proxy.

## For developers: the API

All under `/api/`, JSON, `Authorization: Bearer <device token>` unless
noted. Keys are snake_case.

| Route | |
|---|---|
| `GET /api/hello` | (no token) the computer's id and name |
| `POST /api/pair/claim` | (no token) `{code, device_name, device_id, platform}` → `{claim, compare, computer}` |
| `GET /api/pair/wait?claim=` | (no token) long-poll → `{status, compare, token?}`; the token is given once |
| `GET /api/state?since=N` | long-poll snapshot: `busy, task{id,text,origin,state,steps[{label,status,detail}],reply}, approvals[], jobs[], history[], work_mode, power, screen_view, viewing, computer, device, events_last` |
| `POST /api/ask` | `{text}` → `{ok, message, task_id}` |
| `POST /api/approve` | `{id, allow}` |
| `POST /api/task/pause` · `resume` · `stop` | |
| `GET /api/status` | measured system status; unmeasured values are null |
| `GET /api/tasks`, `/api/tasks/<id>` | history, and one task in full |
| `GET /api/jobs`, `/api/jobs/<id>?lines=` | commands and their output |
| `GET /api/screen?view=full|toby&max=` | JPEG, or 403 `screen_view_off` |
| `GET /api/overview` | open windows |
| `POST /api/window/focus` | `{address}` |
| `GET /api/files` | files the current or last task touched |
| `GET /api/events?after=N&wait=` | notifications |
| `POST /api/work_mode` | `{on}` |
| `GET /api/devices`, `POST /api/devices/revoke {id}`, `POST /api/logout` | |

Task states: thinking, preparing, working, waiting, needs_permission,
paused, completed, failed, cancelled.
