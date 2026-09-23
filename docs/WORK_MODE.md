# Toby Work Mode

Most of what Toby does is quicker without touching the screen: opening an
app, reading a file, running a command. Work Mode is for the rest, the work
that only exists as something on the screen: marking up a PDF in an
annotation tool like Kami, ticking boxes in a form, working a canvas,
clicking through an app that has no other way in. Toby looks at the screen,
finds what it needs, and works on it with a pen, the mouse and the keyboard,
checking the result of each action before the next.

Turn it on in Toby's **Settings → Work Mode**, from the iPhone app's
Computer tab, or just ask ("turn on Work Mode"). It uses the same
mouse-and-keyboard permission as everything else: Toby asks the first time
in a session, on the computer and on your phone.

## The loop

```
look at the screen ──► find the thing ──► act (pen, mouse, keys) ──► look again ──► …
      │                      │                                            │
 screenshot + text      by its words, its number,            did the screen change
 + controls, numbered   a control's name, or x/y             where Toby acted?
```

**Looking** takes a screenshot and builds a numbered list of what's on it:

- the focused app's **controls** (buttons, menu items, fields, checkboxes,
  links, tabs) from the accessibility tree, by name and with exact
  positions;
- the **text** on screen, read with Tesseract, line by line with positions.
  It reads the screenshot and an inverted copy at once, so light text on a
  dark theme is read as well as dark on light.

Toby points at things by number ("#12"), by their words or a number on the
page ("Submit", "42.50"), or by position. If you set `"vision_model"` in
settings.json to a vision model installed in Ollama (such as `qwen2.5vl`),
Toby can also ask it for things without words, like an icon. That's
optional, and slow on a laptop without a GPU.

## What Toby can do

| Tool | For |
|---|---|
| `click_on` | a button, a link, a menu item, a field; double-click too |
| `drag` | moving something, a slider, selecting text |
| `scroll` | the page, or the thing under a target |
| `draw` | circle, underline, strike, highlight, box, check, cross, or an arrow from one thing to another |
| `write_by_hand` | words in handwriting, below, beside, above or at a target |
| `type_text`, `key_press` | typing and shortcuts, as before |

## A real pen

Drawing and handwriting are real input, not an animation on top:

- **Tablet**: a virtual pen tablet made through `/dev/uinput` (needs
  `python-evdev`, which the installer adds). It reports the pen coming near,
  touching and lifting, with pressure that rises at the start of a stroke
  and eases off at the end. Apps that understand pens (drawing apps, and
  browsers, which pass pen input to web apps like Kami) see a stylus.
  Used automatically when there's a single monitor.
- **Mouse**: the left button held down while the pointer moves, through
  ydotool. Every app that lets you draw with a mouse accepts it. Used with
  several monitors, or if the tablet can't be made. Set
  `"work_mode_pen": "mouse"` or `"tablet"` in settings.json to choose.

Handwriting uses Hershey fonts, which are made of single pen strokes, so
the pen writes letters the way a hand does rather than tracing outlines:
Script Simplex for handwriting, Futura Light for print.

Shapes are drawn loosely, like a person marking up a page: a circle goes
round and overlaps where it began; an underline sags a little.

## Checking its work

After drawing or writing, Toby takes another screenshot and compares the
area it drew on. If the page changed there, it says so. If nothing
changed, it says that too, and why that usually happens: the app needs
its pen or highlighter tool selected first (in Kami, choose the pen or
highlighter, or ask Toby to click it), or the app doesn't accept drawing.
It never reports ink that didn't appear.

## Toby's pen pointer

In Work Mode, instead of the walking chibi, Toby shows a small pen in its
own colours at the pointer:

| State | Looks like |
|---|---|
| resting | the pen |
| looking at the screen | a soft glow round the nib |
| about to click | a dashed outline round the target |
| drawing, dragging | the pen tilted in, pressing |
| clicking | a quick press with a ring |

It sits on the click-through overlay layer, so it never gets in the way,
and goes away by itself a couple of seconds after the last move. Your own
cursor stays visible underneath: hiding the system cursor would leave you
without one if Toby ever stopped unexpectedly.

## Limits, honestly

- Text recognition is good on documents and ordinary interface text, and
  weaker on tiny, stylised or low-contrast text. Controls in apps that
  publish an accessibility tree (GTK, Qt, and browsers when accessibility
  is on) are found by name regardless. Chromium-based browsers may need
  `--force-renderer-accessibility` to publish their tree.
- On Wayland, apps don't know where their windows are, so control
  positions are taken relative to the window and moved by where Hyprland
  says the window is. Windows with client-side shadows may be a few pixels
  off.
- The tablet maps onto the whole desktop, which is why it's only used
  automatically with one monitor.
- Work Mode writes what you ask it to write. It won't compose answers to
  homework, worksheet or test questions and fill them in; it will explain
  the material instead.
