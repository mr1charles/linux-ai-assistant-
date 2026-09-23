"""A small GTK window for the accessibility test: a heading, a sentence,
and two coloured buttons whose white labels OCR can't read."""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

css = Gtk.CssProvider()
css.load_from_data(b"button { background: #3366e6; color: white; border: none; padding: 12px 24px; }")
Gtk.StyleContext.add_provider_for_screen(__import__("gi.repository.Gdk", fromlist=["Gdk"]).Screen.get_default(),
                                         css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
win = Gtk.Window(title="Worksheet")
win.set_default_size(600, 300)
win.move(100, 80)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20, margin=30)
box.pack_start(Gtk.Label(label="Total due: 42.50"), False, False, 0)
row = Gtk.Box(spacing=20)
for name in ("Submit", "Cancel"):
    row.pack_start(Gtk.Button(label=name), False, False, 0)
box.pack_start(row, False, False, 0)
win.add(box)
win.connect("destroy", Gtk.main_quit)
win.show_all()
print("ready", flush=True)
Gtk.main()
