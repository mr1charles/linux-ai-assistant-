"""Fingerprint approval, against stand-ins for fprintd-verify."""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import fingerprint as fpm  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


check("a match", fpm.classify("Using device /net/reactivated/Fprint/Device/0\nVerify result: verify-match (done)"), "match")
check("a miss", fpm.classify("Verify result: verify-no-match (done)"), "no-match")
check("anything else is an error, never a match", fpm.classify("Impossible to verify: no devices"), "error")

workdir = tempfile.mkdtemp()


def fake(name, body):
    path = os.path.join(workdir, name)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, 0o755)
    return path


def run(command, cancel_after=None, timeout=5):
    done = threading.Event()
    result = []

    def on_result(outcome):
        result.append(outcome)
        done.set()

    scan = fpm.FingerprintScan(on_result, command=[command], timeout=timeout).start()
    if cancel_after is not None:
        time.sleep(cancel_after)
        scan.cancel()
    done.wait(10)
    return result[0] if result else None


check("a matching finger approves", run(fake("ok", 'echo "Verify result: verify-match (done)"')), "match")
check("a wrong finger doesn't", run(fake("no", 'echo "Verify result: verify-no-match (done)"')), "no-match")
check("a reader error isn't a match", run(fake("err", 'echo "no devices available"; exit 1')), "error")
check("a missing fprintd isn't a match", run(os.path.join(workdir, "does-not-exist")), "error")
check("cancelling mid-scan ends it as cancelled",
      run(fake("slow", 'sleep 10; echo "verify-match"'), cancel_after=0.3), "cancelled")
started = time.monotonic()
check("a scan that never finishes times out, not approves",
      run(fake("hang", "sleep 30"), timeout=1), "error")
check("and the timeout is honoured", time.monotonic() - started < 5, True)

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("fingerprint checks passed")
