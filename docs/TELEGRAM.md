# Little Toby on Telegram

Talk to Toby from any phone with Telegram: nothing to sideload, no Apple
ID, no re-signing every 7 days. You get the same Toby as in the phone app:
tasks with their steps as they happen, Allow and Don't allow buttons for
anything that changes something, the reply, and notifications.

```
Telegram on your phone ──► Telegram's servers ◄── toby-telegram.service (only outgoing connections)
                                                          │  its own device token
                                                          ▼
                                   Toby's phone connection on 127.0.0.1 (never on the internet)
                                                          │
                                                          ▼
                                    Toby: permission levels, approvals, tools, jobs
```

## Setting it up

```bash
toby telegram setup
```

1. In Telegram, open **@BotFather**, send `/newbot`, and choose a name.
   BotFather replies with a token (like `123456789:AAF…`).
2. Paste it when `toby telegram setup` asks (it isn't shown as you type).
3. It shows a one-time code, a link and a QR code. Open the link on your
   phone (or send `/pair CODE` to your bot).
4. The terminal shows the Telegram account that sent the code. Say **y** if
   it's you. Only accounts you confirm at the computer can use the bot;
   anyone else who finds it is ignored without a reply.

That's all. It runs as its own small service (`toby-telegram.service`), so it
starts with your computer like the rest of Toby.

## Using it

Just write to your bot like you would to Toby: "is my game still running?",
"run the tests in ~/project and tell me if they pass", "how's the build
going?". You can also send a **voice note**. It's turned into text on your
computer with the same offline speech model as Voice Mode, and it needs
`ffmpeg`, which most systems already have.

| Command | |
|---|---|
| `/status` | the computer's real CPU, memory, battery and uptime, and what Toby is doing |
| `/tasks` | what you've asked lately and how each went |
| `/jobs` | commands running in the background |
| `/pause`, `/resume`, `/stop` | the task in progress |
| `/screen` | a screenshot, only if you've allowed it (below) |
| `/workmode on`, `off` | Work Mode |
| `/queue`, `/queue <task>` | what's queued for tonight, or add a task ([the overnight queue](QUEUE.md)) |

Anything that needs your OK arrives as a message with **Allow** and **Don't
allow** buttons. It's also shown on the computer and any paired phone, and
the first answer counts. A restricted action (like sudo) asks twice. Ten
minutes without an answer is a no.

You're also told when a watched build or download finishes, and when the
computer goes to sleep.

## Privacy

Telegram bot chats are **not end-to-end encrypted**: Telegram's servers
carry your messages and Toby's replies. That's the trade for not needing an
app. So:

- screenshots are never sent unless you set `"telegram_screenshots": true`
  in `~/linux-agent/settings.json` (and turn on Screen view);
- for a fully private connection, use the iPhone or web app over Tailscale
  (`toby phone on`) instead of, or as well as, Telegram.

Nothing on your computer listens to the internet for Telegram. The service
only makes outgoing connections to Telegram's servers, and it reaches Toby on
127.0.0.1 with its own device token, which you can revoke at any time.

## Managing it

| | |
|---|---|
| `toby telegram status` | the bot, whether it's on, and which accounts can use it |
| `toby telegram pair` | pair another Telegram account |
| `toby telegram unpair <name or id>` | remove an account |
| `toby telegram off` | cut the bot off from Toby (`setup` turns it back on) |
| `toby phone devices` | the bot shows up here too, as a paired device |

If something isn't working: `toby doctor`, or
`journalctl --user -u toby-telegram -n 30`.

The setup is saved in `~/linux-agent/telegram.json`, which only you can read.
It holds the bot's token and Toby's token for it, so never share it or
commit it anywhere.
