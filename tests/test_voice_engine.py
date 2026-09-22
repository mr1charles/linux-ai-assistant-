"""Checks for the parts of voice_engine.py that need no microphone.

The speech queue is the interesting one. Toby speaks a reply sentence by
sentence while the model is still generating it, so the ordering and
interrupt behaviour of that queue is the difference between a reply that
sounds like a sentence and one that sounds like a stutter. espeak-ng is
replaced with a recorder here, so the test observes exactly what would have
been spoken, in what order, without making a sound.
"""
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import voice_engine  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


class FakeProc:
    """Stands in for an espeak-ng process that takes a moment to speak."""

    def __init__(self, duration):
        self._done_at = time.monotonic() + duration

    def poll(self):
        return None if time.monotonic() < self._done_at else 0

    def wait(self):
        remaining = self._done_at - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        return 0

    def terminate(self):
        self._done_at = time.monotonic()


def install_recorder(duration=0.05):
    spoken = []
    lock = threading.Lock()

    def fake_popen(argv, **kwargs):
        with lock:
            spoken.append(argv[-1])
        return FakeProc(duration)

    voice_engine.subprocess.Popen = fake_popen
    return spoken


real_popen = subprocess.Popen

# -- sentences are spoken in order, and none is cut off by the next ----------
spoken = install_recorder()
states = []
engine = voice_engine.VoiceEngine(on_state=states.append)
for sentence in ["First sentence.", "Second sentence.", "Third sentence."]:
    engine.speak(sentence)

deadline = time.monotonic() + 5
while time.monotonic() < deadline and len(spoken) < 3:
    time.sleep(0.01)

check("every queued sentence is spoken", len(spoken), 3)
check("spoken in the order they were queued", spoken,
      ["First sentence.", "Second sentence.", "Third sentence."])
check("speaking is announced exactly once for a run of sentences",
      states.count("speaking"), 1)

# it should report itself done once the queue drains
deadline = time.monotonic() + 5
while time.monotonic() < deadline and "idle" not in states:
    time.sleep(0.01)
check("reports idle once the queue drains", "idle" in states, True)
engine.stop()

# -- an interrupt drops everything still queued -----------------------------
spoken = install_recorder(duration=2.0)
engine = voice_engine.VoiceEngine()
engine.speak("This one starts playing.")
engine.speak("This one should never be spoken.")
engine.speak("Nor this one.")

deadline = time.monotonic() + 5
while time.monotonic() < deadline and not spoken:
    time.sleep(0.01)
engine._kill_tts()   # what a barge-in does
time.sleep(0.4)

check("interrupt discards the rest of the queue", spoken, ["This one starts playing."])
check("not speaking after an interrupt", engine.is_speaking(), False)
engine.stop()

# -- is_speaking stays true across the gap between queued sentences ----------
spoken = install_recorder(duration=0.15)
engine = voice_engine.VoiceEngine()
engine.speak("One.")
engine.speak("Two.")
deadline = time.monotonic() + 5
while time.monotonic() < deadline and not spoken:
    time.sleep(0.01)
check("still counts as speaking while more is queued", engine.is_speaking(), True)
engine.stop()

# -- empty and whitespace-only text is ignored ------------------------------
spoken = install_recorder()
engine = voice_engine.VoiceEngine()
engine.speak("")
engine.speak("   \n ")
time.sleep(0.3)
check("blank text is never sent to the synthesizer", spoken, [])
engine.stop()

subprocess.Popen = real_popen
voice_engine.subprocess.Popen = real_popen

# -- the RMS helper, which the voice-activity threshold depends on ----------
check("rms of silence is zero", voice_engine._rms(b"\x00\x00" * 100), 0.0)
check("rms of empty input is zero", voice_engine._rms(b""), 0.0)
check("rms of an odd-length buffer does not raise",
      voice_engine._rms(b"\x00\x00\x00") == 0.0, True)
loud = voice_engine._rms((3000).to_bytes(2, "little", signed=True) * 100)
check("rms of a loud tone is above the speech threshold",
      loud > voice_engine.VAD_THRESHOLD, True)

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("voice engine checks passed")
