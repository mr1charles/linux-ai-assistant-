"""
voice_engine.py — offline voice pipeline for Little Toby's Voice Mode.

Continuous mic capture + energy-based VAD + Vosk speech-to-text + espeak-ng
text-to-speech with barge-in interruption. Runs on its own background thread;
talks to the GTK app only through callbacks. This module never imports Gtk —
the caller is responsible for marshaling any callback that touches widgets
through GLib.idle_add, same as the rest of the app's background threads.

Honesty note on scope: "emotion detection from voice" in the original design
brief is NOT implemented here as real acoustic/prosody analysis — that needs
a trained audio classifier, a real dependency and an accuracy commitment
this project doesn't have. Toby's "mood" already comes from the LLM reading
the *text* of what you said; Voice Mode reuses that signal rather than
faking a second, fabricated audio-based one. Voice activity detection and
word-confidence, on the other hand, ARE real here — VAD is a genuine
energy-threshold read of the raw mic samples, and confidence comes straight
from Vosk's own per-word scores.
"""

import array
import json
import math
import os
import queue
import subprocess
import threading
from pathlib import Path

VOSK_MODEL_PATH = os.environ.get("VOSK_MODEL_PATH", str(Path.home() / "linux-agent" / "vosk-model-small-en-us-0.15"))
WAKE_WORD = os.environ.get("WAKE_WORD", "toby")

SAMPLE_RATE = 16000
VAD_THRESHOLD = 500        # int16 RMS threshold for "someone is talking"
INTERRUPT_RMS_STREAK = 3   # consecutive loud chunks while Toby is speaking = a real interruption

VOICE_PRESETS = ["en-us", "en-us+f3", "en-gb", "en-gb-x-rp", "en+m3"]


def _rms(data: bytes) -> float:
    if not data:
        return 0.0
    usable = data[: len(data) - (len(data) % 2)]
    if not usable:
        return 0.0
    samples = array.array("h")
    samples.frombytes(usable)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class VoiceEngine:
    def __init__(self, on_partial=None, on_final=None, on_level=None, on_state=None):
        self.on_partial = on_partial   # (text) -> None
        self.on_final = on_final       # (text, avg_confidence_0_to_1) -> None
        self.on_level = on_level       # (level_0_to_1) -> None, drives the waveform widget
        self.on_state = on_state       # (state_str) -> None: idle/listening_wake/listening_command/speaking/error

        self.enabled = False
        self.wake_word_enabled = True
        self.voice = "en-us"
        self.rate = 165
        self.pitch = 50

        self._thread = None
        self._stop_flag = threading.Event()
        self._tts_proc = None
        self._tts_lock = threading.Lock()
        self._push_to_talk_flag = threading.Event()
        self._model = None

    # -- lifecycle ------------------------------------------------------------
    def start(self):
        if self.enabled:
            return
        if not os.path.isdir(VOSK_MODEL_PATH):
            if self.on_state:
                self.on_state("error: vosk model not found at " + VOSK_MODEL_PATH)
            return
        self.enabled = True
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.enabled = False
        self._stop_flag.set()
        self._kill_tts()
        if self.on_state:
            self.on_state("idle")

    def push_to_talk_start(self):
        self._push_to_talk_flag.set()

    def push_to_talk_stop(self):
        self._push_to_talk_flag.clear()

    # -- text-to-speech, with barge-in interruption support --------------------
    def speak(self, text):
        if not text or not text.strip():
            return
        self._kill_tts()
        if self.on_state:
            self.on_state("speaking")
        with self._tts_lock:
            try:
                self._tts_proc = subprocess.Popen(
                    ["espeak-ng", "-v", self.voice, "-s", str(self.rate), "-p", str(self.pitch), text],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                self._tts_proc = None
                if self.on_state:
                    self.on_state("error: espeak-ng not installed")

    def _kill_tts(self):
        with self._tts_lock:
            if self._tts_proc and self._tts_proc.poll() is None:
                self._tts_proc.terminate()
            self._tts_proc = None

    def is_speaking(self) -> bool:
        with self._tts_lock:
            return self._tts_proc is not None and self._tts_proc.poll() is None

    # -- main audio loop ---------------------------------------------------------
    def _run(self):
        import sounddevice as sd
        from vosk import KaldiRecognizer, Model

        if self._model is None:
            try:
                self._model = Model(VOSK_MODEL_PATH)
            except Exception as e:
                if self.on_state:
                    self.on_state(f"error: couldn't load speech model ({e})")
                self.enabled = False
                return

        rec = KaldiRecognizer(self._model, SAMPLE_RATE)
        try:
            rec.SetWords(True)  # ask Vosk for per-word confidence scores
        except Exception:
            pass

        q = queue.Queue()

        def callback(indata, frames, time_info, status):
            q.put(bytes(indata))

        loud_streak = 0
        awaiting_command = False
        state_reported = None

        try:
            stream_ctx = sd.RawInputStream(
                samplerate=SAMPLE_RATE, blocksize=4000, dtype="int16", channels=1, callback=callback
            )
        except Exception as e:
            if self.on_state:
                self.on_state(f"error: microphone unavailable ({e})")
            self.enabled = False
            return

        with stream_ctx:
            while not self._stop_flag.is_set():
                try:
                    data = q.get(timeout=0.5)
                except queue.Empty:
                    continue

                level_raw = _rms(data)
                if self.on_level:
                    self.on_level(min(1.0, level_raw / 4000.0))

                if self.is_speaking():
                    # Don't feed our own TTS output back into Vosk — just watch
                    # raw energy for a genuine barge-in (the user talking over Toby).
                    if level_raw > VAD_THRESHOLD:
                        loud_streak += 1
                        if loud_streak >= INTERRUPT_RMS_STREAK:
                            self._kill_tts()
                            loud_streak = 0
                            awaiting_command = True
                    else:
                        loud_streak = 0
                    continue
                loud_streak = 0

                push_to_talk = self._push_to_talk_flag.is_set()
                listening_for_command = awaiting_command or push_to_talk or not self.wake_word_enabled
                desired_state = "listening_command" if listening_for_command else "listening_wake"
                if desired_state != state_reported and self.on_state:
                    self.on_state(desired_state)
                    state_reported = desired_state

                if rec.AcceptWaveform(data):
                    result = json.loads(rec.Result())
                    text = result.get("text", "").strip()
                    words = result.get("result", [])
                    if words:
                        avg_conf = sum(w.get("conf", 1.0) for w in words) / len(words)
                    else:
                        avg_conf = 1.0 if text else 0.0
                    if not text:
                        continue
                    if listening_for_command:
                        if self.on_final:
                            self.on_final(text, avg_conf)
                        awaiting_command = False
                    elif WAKE_WORD.lower() in text.lower():
                        remainder = text.lower().replace(WAKE_WORD.lower(), "", 1).strip()
                        # strip common filler words left over from "hey toby" /
                        # "ok toby" style phrasing so the model sees a clean
                        # command instead of a leading "hey"
                        for filler in ("hey", "hi", "ok", "okay", "yo"):
                            if remainder.startswith(filler + " "):
                                remainder = remainder[len(filler):].strip()
                            elif remainder.endswith(" " + filler):
                                remainder = remainder[: -len(filler)].strip()
                        if remainder:
                            if self.on_final:
                                self.on_final(remainder, avg_conf)
                        else:
                            awaiting_command = True
                            if self.on_state:
                                self.on_state("listening_command")
                            state_reported = "listening_command"
                else:
                    partial = json.loads(rec.PartialResult()).get("partial", "")
                    if partial and self.on_partial:
                        self.on_partial(partial)
