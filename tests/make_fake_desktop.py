"""Draw a stand-in desktop screenshot for the fold tests and previews.

A real screenshot isn't available off the real machine, and the fold needs
something with recognisable structure — a bar across the top, windows with
title bars, text — so that a wrong transform is obvious at a glance.
"""
import math
import sys

import cairo


def make(path, width=1920, height=1080):
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(s)
    bg = cairo.LinearGradient(0, 0, width, height)
    bg.add_color_stop_rgb(0, 0.10, 0.12, 0.28)
    bg.add_color_stop_rgb(1, 0.30, 0.10, 0.32)
    cr.set_source(bg)
    cr.paint()
    # a grid, so bending is visible everywhere
    cr.set_source_rgba(1, 1, 1, 0.08)
    cr.set_line_width(1)
    for x in range(0, width, 80):
        cr.move_to(x, 0); cr.line_to(x, height)
    for y in range(0, height, 80):
        cr.move_to(0, y); cr.line_to(width, y)
    cr.stroke()
    # top bar
    cr.set_source_rgba(0.04, 0.05, 0.09, 0.9)
    cr.rectangle(0, 0, width, 36); cr.fill()
    cr.set_source_rgb(0.9, 0.9, 1)
    cr.select_font_face("sans-serif"); cr.set_font_size(16)
    cr.move_to(20, 24); cr.show_text("Workspace 1        Tue 22 Sep  10:41")
    # windows
    for (x, y, w, h, c) in [(120, 120, 900, 560, (0.93, 0.93, 0.96)),
                             (820, 300, 900, 620, (0.14, 0.15, 0.2))]:
        cr.set_source_rgba(0, 0, 0, 0.35)
        cr.rectangle(x + 10, y + 16, w, h); cr.fill()
        cr.set_source_rgb(*c)
        cr.rectangle(x, y, w, h); cr.fill()
        cr.set_source_rgb(0.35, 0.55, 1.0)
        cr.rectangle(x, y, w, 34); cr.fill()
        dark = c[0] < 0.5
        cr.set_source_rgb(*(0.85, 0.85, 0.9) if dark else (0.15, 0.15, 0.2))
        cr.set_font_size(20)
        for i in range(12):
            cr.move_to(x + 24, y + 80 + i * 36)
            cr.show_text("Text analysis essay: every claim needs quoted evidence." [: 20 + (i * 7) % 40])
    # Toby's pill at the bottom
    cr.set_source_rgba(0.04, 0.05, 0.09, 0.85)
    cr.arc(width / 2 - 300, height - 90, 28, math.pi / 2, 3 * math.pi / 2)
    cr.arc(width / 2 + 300, height - 90, 28, -math.pi / 2, math.pi / 2)
    cr.close_path(); cr.fill()
    cr.set_source_rgb(1, 0.75, 0.25)
    cr.arc(width / 2 - 290, height - 90, 20, 0, 2 * math.pi); cr.fill()
    s.write_to_png(path)


if __name__ == "__main__":
    make(sys.argv[1] if len(sys.argv) > 1 else "fake_desktop.png")
