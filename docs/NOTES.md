# Your notes as Toby's memory

Point Toby at your folder of Markdown notes (an Obsidian vault, or any
folder of `.md` and `.txt` files) and it becomes part of what Toby knows.

```bash
toby notes set ~/Obsidian
```

(or put the folder in Toby's **Settings, Notes folder**.)

## What Toby does with them

**Answers from them.** When you ask something your notes might answer ("when
is the dentist?", "what did I decide about the car?"), Toby looks through
them and puts the few best-matching passages in front of the model, with the
note's name, so the answer comes from what you wrote rather than a guess.
You can also ask directly: "search my notes for the wifi setup".

**Keeps its memory there.** What Toby learns about you (when you tell it
something about yourself, or say "remember that…") lives in
`Toby/Memory.md` in your notes: a plain list under headings, one fact per
line. That file *is* Toby's memory. The Memories graph shows it, and a line
you edit or delete in your notes app is changed or forgotten. The first time,
what Toby already knew is copied there. The old `~/linux-agent/knowledge.json`
is left as it was, as a backup, and it's used again if you turn notes off.

**Writes down what it did.** Each task gets one line in `Toby/Log/<date>.md`
("14:05 run the tests in my project: all 5 passed"). Turn that off with
`"notes_daily_log": false` in settings.json.

**Saves notes you ask for** in `Toby/Notes/`: "make a note of gift ideas for
Mum: a teapot" becomes a note there (say `Gift ideas for Mum.md`), and asking
again with the same title adds to it. Toby only ever adds to its own `Toby/` folder; it
doesn't edit your other notes unless you ask it to write a file, and that
asks first like any other change.

## What Toby never reads

- hidden folders: `.obsidian`, `.trash`, `.git`;
- folders whose name says they're private: Private, Passwords, 2FA, Secrets,
  Finance, Health, Medical, Credentials (and names like "Personal Finance"
  or "Health-2026");
- anything you list in a `.tobyignore` file at the top of the folder, one
  pattern per line, like `.gitignore`:

  ```
  Journal/
  Work/Clients/
  *.private.md
  ```

Text that looks like a password or a key (`password: …`, `api_key=…`,
`sk-…`, `ghp_…`, private key blocks) is replaced with `[hidden]` in anything
shown to the model, even in notes it does read.

## How it works

Everything stays on the computer. The search is a full-text index (SQLite's
FTS5, ranked with BM25) in `~/linux-agent/notes_index.sqlite`, readable by
you alone. Only notes that changed are read again, checked at most once a
minute. It needs no extra packages and no AI model. As a guide, on this
project's test machine 3,000 notes (21 MB) took about 3 seconds to read the
first time, and a search takes about 15 ms.

Passages are only added to Toby's prompt when they cover most of what you
asked, so "hello" or "good morning" don't drag in a note that happens to
share a word. On a slow computer every extra line of prompt is a wait before
the answer.

## Commands

| | |
|---|---|
| `toby notes set <folder>` | use a notes folder (reads it straight away and says how many notes) |
| `toby notes status` | the folder, how many notes, where the memory is |
| `toby notes search <words>` | try the search yourself |
| `toby notes off` | stop reading the folder; nothing in it is changed or deleted |
