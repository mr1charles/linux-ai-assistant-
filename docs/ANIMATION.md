# Little Toby's animation system

How Toby moves, where each animation lives, and how to add a new one
without making the whole thing feel like a pile of unrelated effects.

## The language

Every animation should feel smooth, fast, responsive and natural, with a
little character, because Toby is a companion rather than a toolbar. In
practice that means:

- **Things arrive quickly and settle softly.** Most of the motion happens in
  the first third, then it eases into place. Nothing overshoots and springs
  back; that reads as a toy.
- **Things leave faster than they arrive.** Nobody wants to wait for a panel
  to finish going away.
- **Nothing makes you wait.** The longest ordinary transition is about half
  a second, and most are a quarter of one.
- **Interrupting is always safe.** Reversing an animation halfway continues
  from exactly where it is. Nothing jumps back to its start.
- **Character lives in Toby, not the chrome.** The face breathes, blinks,
  glances, squashes when tapped and hops when a task is done. The chibi
  walks, reaches and cheers. Panels, cards and windows stay calm.

## Tokens: one definition, four consumers

`scripts/toby_anim.py` defines four curves and five durations. They are
used, unchanged, by the Python animations, the GTK stylesheet's
transitions, Hyprland's window animations (`hypr_animations.py`) and the
phone app's CSS custom properties. `tests/test_motion_tokens.py` fails if
any of them drift.

| Curve | For | Control points |
|---|---|---|
| `enter` | something arriving | 0.16, 1.0, 0.3, 1.0 |
| `exit` | something leaving | 0.55, 0.0, 0.8, 0.2 |
| `move` | travelling between two resting places | 0.33, 1.0, 0.68, 1.0 |
| `standard` | small state changes: hover, press, colour | 0.2, 0.0, 0.0, 1.0 |

| Duration | Seconds | For |
|---|---|---|
| `instant` | 0.09 | press and hover feedback |
| `quick` | 0.16 | small things appearing |
| `standard` | 0.24 | panels and cards leaving |
| `emphasized` | 0.34 | panels and cards arriving; the pill |
| `gentle` | 0.52 | large surfaces, and Toby's own gestures |

No curve in the system overshoots; `tests/test_motion.py` checks that too.

## The engine

`toby_anim.Animator` runs every transition in the app, through one
instance called `MOTION` in `linux_agent_apple.py`. It is:

- **time-based**, so a busy CPU makes an animation choppier, never longer;
- **continuous**, so animating a value that is already moving starts from
  where it is right now;
- **frame-independent**, so it finishes on the clock even when the
  compositor stops sending frames (a locked screen, a display turning off);
- **cancellable**, so a superseded animation never runs its "done"
  callback (a hide interrupted by a show doesn't go on to hide);
- **idle when idle**: its timer exists only while something is moving;
- **reduced-motion aware**: with Settings, Animations, Reduce motion on, or
  GTK's system-wide `gtk-enable-animations` off, everything jumps straight to
  its end state and still reports that it finished.

Two helpers cover almost every window:

```python
reveal(window, surface, "key", edge, margin_from, margin_to)   # fade + short travel in
dismiss(window, surface, "key", edge, margin_to)               # fade + travel out, then hide
```

`surface` is the window's inner container. Opacity goes there rather than
on the window, because GDK ignores opacity on a toplevel under Wayland.

To add an animation, use `reveal`/`dismiss` for a window, or
`MOTION.animate(key, target, apply, duration, curve)` for anything else,
with token names for the duration and curve. Don't add a `GLib.timeout_add`
loop or a new easing function; `tests/test_motion_tokens.py` catches the
patterns this audit removed if they come back.

## What moves, and where

| What | Where | How |
|---|---|---|
| The pill appearing | `show_panel` | rises on `enter` over `emphasized` while fading in; the face forms inside a rainbow ring flash |
| The pill leaving | `hide_panel` | fades and sinks on `exit`; summoning it again mid-sink turns it around |
| The face, idle | `Face.tick` | breathing, blinking, drifting gaze toward the pointer; a third of the frame rate when nothing fast is happening |
| The face, touched | `Face.tap`, `pulse_happy` | a damped spring: squash on click, a small hop when a task finishes |
| The face, reacting | `Face.react` | glances toward a workspace switch, looks up when a window opens; only when idle |
| The face, thinking | `_draw_thinking_motes` | three slow motes drifting around the head, in place of a spinner |
| Waiting for a reply | `_start_thinking_dots` | the word "Thinking" breathes gently until the first words arrive |
| Dynamic Island | `show_island`, `hide_island` | drops in from the top edge, leaves upward; a progress ring fills as task steps finish |
| Island task view | `IslandExpanded.open/close` | fades and settles below the island |
| Sidebar | `toggle_expanded` | fades and settles down from above, `gentle` |
| Sidebar pages | `content_stack` | cross-fade, `quick` |
| Study Helper | `open_helper/close_helper` | slides in from the right edge |
| Quiz, flashcards, topic cards | `open_review`, `open_topic` | fade in; fade out when closed |
| Hover, press, focus | stylesheet | `instant` on `standard` |
| The chibi | `chibi.py` | emerges from the pill, grows a body, walks, reaches, types, cheers, flies home; the one place a small overshoot is allowed, as the body grows, because it's character animation |
| Mouse glides Toby makes | `screen_control.move` | time-based on `move` |
| Lid close and open | `fold_effect.py`, `toby_fold.py` | compress, lean, fold to the hinge, black; the reverse on waking |
| Shutdown | `toby_fold.py` | the desktop fades to black |
| Login | `_startup_greeting` | the pill rises, says hello once per login, and tucks away |
| Windows, workspaces, menus, minimise | Hyprland, via `toby animations on` | the same curves, applied at runtime only |
| The phone app | `mobile/app.css` | the same curves and durations as CSS custom properties |

## Fullscreen

While a window is fullscreen (a video, a game), Toby holds back anything
you didn't ask for. Reply cards wait until you leave fullscreen, and the
login greeting is skipped. Things you asked for, like the chibi doing a
task, still happen.

## What the audit changed

Before this pass, most animations in the app each had their own timer and
curve:

- The Dynamic Island and the Study Helper slid by counting 10 to 12 steps of
  14 ms, so a busy machine made them slower. Reversing one halfway snapped it
  back to its starting edge first, a jump of about 70 px (a test now checks
  for this).
- The pill slid up by counting frames on the classic overshooting "back"
  curve: the cheap bounce.
- The main file defined its own copies of the easing functions, and the
  mouse glide defined another.
- The sidebar, the task view and the study windows appeared and vanished
  with no transition at all.
- Waiting for a reply showed cycling "Thinking..." dots, a spinner in text
  form.
- Stylesheet transitions used plain `ease-out` at mixed durations; the
  phone app used a different curve again.

All of those now go through the tokens and the engine described above.

## Cost

Measured on the development machine, in software rendering:

| | Cost |
|---|---|
| Pill open and idle | about 4% of one CPU core (it was 18% before the shadow and frame-rate work) |
| Pill hidden | under 1% |
| The chibi on screen | about 2 ms a frame, only while it's out |
| The lid fold | about 7 ms a frame at 1080p, for about 0.6 s closing and 0.7 s opening |
| The wake ring | about 4 ms a frame, for 0.7 s |

`tests/perf_idle.py` guards the idle numbers.

## What still needs a real machine

The engine, the curves and every window's transitions are exercised under
Xvfb. What only a live Hyprland can confirm is how layer-shell margin
changes look on screen at the compositor's real frame rate, and whether
Hyprland's own layer animations should be turned off for Toby's surfaces
if you prefer them to be Toby's alone.
