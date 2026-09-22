"""
fingerprint.py — approve Toby's permission prompts with your fingerprint.

When Toby first needs the mouse, the keyboard, or to open a package install
in this session, it asks. With this turned on (Settings, off by default),
touching the laptop's fingerprint reader answers "yes" — the same as
pressing the button, and the button still works.

It uses fprintd, the standard Linux fingerprint service, through its own
``fprintd-verify`` command, so Toby never sees or stores anything about your
fingerprint: fprintd does the match and reports only "matched" or "didn't".
A scan that doesn't match simply doesn't approve; it never counts as "no".

Everything here is bounded: a scan gives up after a timeout, can be
cancelled the moment the prompt is answered some other way, and a missing
reader or missing fprintd just means the option isn't offered.
"""

import getpass
import os
import shutil
import signal
import subprocess
import threading

VERIFY_TIMEOUT_S = 20


def reader_available():
    """True if fprintd is installed and you have a finger enrolled."""
    if not shutil.which("fprintd-verify") or not shutil.which("fprintd-list"):
        return False
    try:
        out = subprocess.run(["fprintd-list", getpass.getuser()], capture_output=True,
                             text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = (out.stdout + out.stderr).lower()
    if "no devices" in text or "no fingers" in text or "has no fingers enrolled" in text:
        return False
    return out.returncode == 0 and "finger" in text


def classify(output):
    """Map fprintd-verify's output to "match", "no-match" or "error"."""
    text = output.lower()
    if "verify-match" in text:
        return "match"
    if "verify-no-match" in text:
        return "no-match"
    return "error"


class FingerprintScan:
    """One scan, run on its own thread; on_result(outcome) when it ends.

    outcome is "match", "no-match", "error" or "cancelled". on_result is
    called on the scan thread — marshal to GTK yourself.
    """

    def __init__(self, on_result, command=("fprintd-verify",), timeout=VERIFY_TIMEOUT_S):
        self.on_result = on_result
        self.command = list(command)
        self.timeout = timeout
        self._proc = None
        self._cancelled = threading.Event()
        self._lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="fingerprint").start()
        return self

    def cancel(self):
        self._cancelled.set()
        with self._lock:
            self._kill()

    def _kill(self):
        """Stop the scan and anything it started.

        The scan runs in a process group of its own and the whole group is
        killed: killing only the top process can leave a child holding the
        output pipe open, and reading that pipe would then wait for the
        child — the cancel or timeout would not really take effect.
        """
        if self._proc and self._proc.poll() is None:
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    def _run(self):
        try:
            with self._lock:
                if self._cancelled.is_set():
                    raise _Cancelled()
                self._proc = subprocess.Popen(self.command, stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT, text=True,
                                              start_new_session=True,
                                              env={**os.environ, "LC_ALL": "C"})
            try:
                output, _ = self._proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                with self._lock:
                    self._kill()
                self._proc.communicate()
                outcome = "error"
            else:
                outcome = "cancelled" if self._cancelled.is_set() else classify(output)
        except _Cancelled:
            outcome = "cancelled"
        except OSError:
            outcome = "error"
        try:
            self.on_result(outcome)
        except Exception as e:
            print("FINGERPRINT CALLBACK ERROR:", e, flush=True)


class _Cancelled(Exception):
    pass
