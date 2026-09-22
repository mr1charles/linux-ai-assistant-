"""The fast path must be exactly right on the requests it takes and must
decline everything else. Declining too much only costs speed; taking a
request it shouldn't means doing the wrong thing instantly."""
import datetime
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import fast_path as fp  # noqa: E402

failures = []
APPS = {"firefox", "discord", "spotify", "terminal", "code"}
NOW = datetime.datetime(2026, 9, 22, 15, 5)


def actions(text):
    r = fp.match(text, APPS, NOW)
    return None if r is None else r["actions"]


def expect(text, want):
    got = actions(text)
    if got != want:
        failures.append(f"{text!r}: got {got!r}, want {want!r}")


yt = {"tool": "open_url", "url": "https://www.youtube.com"}
expect("open youtube", [yt])
expect("Open YouTube.", [yt])
expect("hey toby open youtube", [yt])
expect("toby, can you please open youtube for me", [yt])
expect("could you open up youtube", [yt])
expect("open youtube and tiktok", [yt, {"tool": "open_url", "url": "https://www.tiktok.com"}])
expect("open google classroom then khan academy",
       [{"tool": "open_url", "url": "https://classroom.google.com"},
        {"tool": "open_url", "url": "https://www.khanacademy.org"}])
expect("launch firefox", [{"tool": "open_app", "command": "firefox"}])
expect("open discord and spotify", [{"tool": "open_app", "command": "discord"},
                                    {"tool": "open_app", "command": "spotify"}])
expect("go to example.com", [{"tool": "open_url", "url": "https://example.com"}])
expect("close this tab", [{"tool": "close_tab"}])
expect("close tab", [{"tool": "close_tab"}])
expect("close this window", [{"tool": "close_active_window"}])
expect("study mode on", [{"tool": "enable_study_mode"}])
expect("start studying", [{"tool": "enable_study_mode"}])
expect("turn off study mode", [{"tool": "disable_study_mode"}])
expect("give me back control", [{"tool": "disable_control"}])
expect("what time is it", [])

r = fp.match("what time is it", APPS, NOW)
if r["reply"] != "It's 3:05 PM.":
    failures.append(f"time reply wrong: {r['reply']!r}")
r = fp.match("what's the date", APPS, NOW)
if r["reply"] != "It's Tuesday, September 22.":
    failures.append(f"date reply wrong: {r['reply']!r}")
r = fp.match("open youtube and tiktok", APPS, NOW)
if r["reply"] != "Opening YouTube and TikTok.":
    failures.append(f"open reply wrong: {r['reply']!r}")

# Everything below needs the model's judgement and must be declined.
for text in [
    "open youtube and find a video about photosynthesis",
    "how much evidence do I need for a text analysis essay",
    "plan out my week",
    "open my english essay",
    "open blender",                      # an app not on the allowed list
    "open youtube and some random thing",
    "close everything",
    "what time is my math class",
    "search google for cell respiration",
    "send a discord message saying I'm live",
    "open",
    "",
    "open " + "youtube and " * 12 + "tiktok",   # too long to be a simple command
    "don't open youtube",
    "why did you open youtube",
]:
    if fp.match(text, APPS, NOW) is not None:
        failures.append(f"should have gone to the model: {text!r}")

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("fast path checks passed")
