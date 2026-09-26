"""`toby telegram setup | status | unpair | off`, against a stand-in Telegram.

Run with HOME pointing at a throwaway folder (tests/run.sh does): it writes
Toby's settings, remote.json and telegram.json there. systemctl is recorded,
not run.
"""

import stat
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))
assert "linux-agent" not in str(Path.home()) and Path.home() != Path("/root"), "run with a throwaway HOME"

import remote_bridge as rb  # noqa: E402
import telegram_bot as tb  # noqa: E402
import toby_cli  # noqa: E402
import toby_settings  # noqa: E402
from test_telegram import OWNER, TOKEN, FakeTelegram, check  # noqa: E402

commands = []


def fake_run(cmd, timeout=10, **kw):
    commands.append(cmd)
    import subprocess
    return subprocess.CompletedProcess(cmd, 0, "inactive\n" if "is-active" in cmd else "", "")


toby_cli.run = fake_run
toby_cli.TELEGRAM_UNIT = Path.home() / ".config" / "systemd" / "user" / "toby-telegram.service"
say_lines = []
toby_cli.say = lambda text="": say_lines.append(str(text))

fake = FakeTelegram()
tb.save_config({"api_base": fake.base})       # point the setup at the stand-in Telegram

# -- a wrong token changes nothing ---------------------------------------------------
rc = toby_cli.cmd_telegram(["setup"], ask=lambda q: "", secret=lambda q: "999:WRONG")
check(rc == 1 and any("didn't accept that token" in s for s in say_lines), "a token Telegram refuses is rejected")
check(not toby_settings.load().get("telegram_enabled") and rb.DeviceStore().devices() == [],
      "and nothing is turned on or paired")

# -- setup with a real token, pairing your account ------------------------------------------
answers = iter(["y"])


def play():
    time.sleep(0.5)
    while not any("/pair" in s for s in say_lines):
        time.sleep(0.1)
    code = next(s for s in say_lines if "/pair" in s).split("/pair", 1)[1].strip()
    fake.say(OWNER, f"/pair {code}")


threading.Thread(target=play, daemon=True).start()
rc = toby_cli.cmd_telegram(["setup"], ask=lambda q: next(answers), secret=lambda q: TOKEN)
config = tb.load_config()
check(rc == 0, "setup finishes")
check(config["bot_username"] == "toby_test_bot" and config["allowed_users"][0]["id"] == OWNER["id"],
      "it knows the bot and your account")
check(stat.S_IMODE(tb.CONFIG_PATH.stat().st_mode) == 0o600, "telegram.json is readable by you alone")
devices = rb.DeviceStore().devices()
check(len(devices) == 1 and devices[0]["platform"] == "telegram" and
      rb.DeviceStore().authenticate(config["device_token"]) is not None,
      "the bot is a paired device of its own, with a token Toby accepts")
check(toby_settings.load()["telegram_enabled"], "Toby's side is switched on")
unit = toby_cli.TELEGRAM_UNIT.read_text()
check(str(toby_cli.REPO) in unit and "@REPO@" not in unit and "TOBY_NO_DESKTOP=1" in unit,
      "the service file points at this copy of Toby")
check(["systemctl", "--user", "enable", "--now", "toby-telegram.service"] in commands, "and the service is started")
check("toby-telegram.service" in toby_cli.services(), "toby start/stop/restart now include it")

# -- running setup again replaces the device rather than piling them up --------------------------
old_token = config["device_token"]
rc = toby_cli.cmd_telegram(["setup"], ask=lambda q: "" if "Keep" in q else "n", secret=lambda q: TOKEN)
check(rc == 0 and len(rb.DeviceStore().devices()) == 1, "setting up again keeps one device")
check(rb.DeviceStore().authenticate(old_token) is None, "and the old token stops working")

# -- status, unpair, off --------------------------------------------------------------------------
say_lines.clear()
toby_cli.cmd_telegram(["status"])
check(any("@toby_test_bot" in s for s in say_lines) and any("Alex" in s for s in say_lines), "status lists the bot and accounts")
check(toby_cli.cmd_telegram(["unpair", "nobody"]) == 1, "unpairing someone who isn't paired says so")
check(toby_cli.cmd_telegram(["unpair", str(OWNER["id"])]) == 0 and tb.load_config()["allowed_users"] == [],
      "unpairing an account removes it")
token = tb.load_config()["device_token"]
check(toby_cli.cmd_telegram(["off"]) == 0, "off works")
check(rb.DeviceStore().authenticate(token) is None and not toby_settings.load()["telegram_enabled"],
      "off revokes the bot's device and switches Toby's side off")
check(["systemctl", "--user", "disable", "--now", "toby-telegram.service"] in commands, "and stops the service")
print("telegram setup checks passed")
