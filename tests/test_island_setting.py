"""The Dynamic Island's off switch: nothing shows, except the notice that a
phone is viewing the screen, which is a privacy signal and always shows."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import _layer_shell_stub  # noqa: E402

_layer_shell_stub.install()
import linux_agent_apple as app  # noqa: E402

island = app.DynamicIsland(on_click=lambda *a: None, on_expand=lambda *a: None)

app.SETTINGS["island_enabled"] = True
island.show_island("Toby is thinking…")
assert island._visible_target, "on: the island shows"
island.hide_island()

app.SETTINGS["island_enabled"] = False
island.show_island("Toby is thinking…")
assert not island._visible_target, "off: the island stays hidden"
assert island.label.get_text() == "Toby is thinking…", "its text still updates, for when it's turned back on"
island.show_island("Your phone is viewing this screen", force=True)
assert island._visible_target, "the phone-viewing notice shows even with the island off"
island.hide_island()
print("island setting checks passed")
