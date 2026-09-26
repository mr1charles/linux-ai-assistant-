"""Toby's phone connection starts for Telegram alone (phone apps off), stays
on this computer, and stays off when neither is set up."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import _layer_shell_stub  # noqa: E402

_layer_shell_stub.install()
import linux_agent_apple as app  # noqa: E402

app.SETTINGS.update(remote_enabled=False, telegram_enabled=False, remote_port=0)
win = app.AssistantWindow(app.RingFlash())
assert win.remote is None, "with neither the phone apps nor Telegram set up, nothing listens"

app.SETTINGS.update(telegram_enabled=True)
win2 = app.AssistantWindow(app.RingFlash())
assert win2.remote is not None, "Telegram set up: the connection starts, with the phone apps off"
assert win2.remote.address[0] == "127.0.0.1", "and only on this computer"
win2.remote.stop()
print("bridge-for-telegram checks passed")
