# The overnight queue

Things for Toby to do while you sleep. Write them in a checklist, and in the
morning they're ticked off with what Toby found next to them.

## Adding tasks

Any of these:

- tell Toby: "tonight, find the five biggest folders in Downloads", "at 3pm,
  check whether my project builds";
- on Telegram: `/queue summarize ~/project/README.md` (`/queue` on its own
  lists what's there);
- in a terminal: `toby queue add "summarize ~/project/README.md"`;
- or edit the file yourself. It's `Toby/Queue.md` in your notes folder if
  you've set one (so you can add to it from your phone in Obsidian),
  otherwise `~/linux-agent/Queue.md` (`toby queue file` prints where).

```markdown
- [ ] Find the five biggest folders in ~/Downloads and list them
- [ ] Read ~/project/README.md and write me a summary of what's left to do
  Keep it short, in bullet points.
- [ ] at 14:30: check whether ~/project builds
```

Indented lines under a task are part of it. A task starting `at HH:MM:`
runs at that time: the next time the clock reaches it after you wrote it,
so "at 03:00" written at 10 pm means tonight. Tasks without a time run in
the night window, 01:00 to 07:00 (`"queue_start"` and `"queue_end"` in
settings.json).

## What happens

Toby starts one task at a time, only when it isn't busy with you, and runs
it exactly like something you asked, with the same tools and the same
permission levels. While a task runs, Toby asks the system not to
idle-suspend.

**Anything that needs your OK is left for you, not done.** Nobody's there
at 3 am to say yes, and waking you isn't the point. So a task that wants to
change or delete files, run a command, or use the mouse and keyboard stops
at that step. It's marked *needs you*, with what it wanted. Looking and
reading work, and so do searching your notes, remembering things, and
writing notes in Toby's own folder. So research, summaries, checks and
reports work well overnight, and the doing is yours in the morning.

When a task finishes, its line is ticked:

```markdown
- [x] Find the five biggest folders in ~/Downloads (done 01:34, see Outbox/2026-09-27 Find the five biggest folders.md)
- [!] Tidy my Downloads (needs you: Delete 3 files from ~/Downloads; see Outbox/…)
- [!] Something that broke (couldn't finish, 02:10; see Outbox/…)
```

What Toby found goes in `Outbox/<date> <task>.md` next to the queue file:
the answer, each step, what was left for you, and the files it touched. In
the morning you get **one** notification with the tally ("From your queue:
3 done, 1 needs you") on paired phones and Telegram, and on the Dynamic
Island if it's on.

## Good to know

- **The computer has to be awake.** A laptop with its lid closed is asleep,
  so nothing runs. Leave it open and plugged in overnight, or run the queue
  whenever you like with `toby queue run`. Tasks that didn't get a turn
  wait for the next night.
- The local model on a laptop CPU is slow, so a task can take several
  minutes. That's fine at night. Each task gets at most 45 minutes.
- If Toby restarts mid-task, that task is tried again.
- Turn the whole thing off with `"queue_enabled": false` in settings.json.

## Commands

| | |
|---|---|
| `toby queue` | what's queued, done, or waiting for you |
| `toby queue add "<task>"` | add a task (start it with `at 14:30:` for a time) |
| `toby queue run` | start now instead of waiting for the night |
| `toby queue file` | where the queue file is |
