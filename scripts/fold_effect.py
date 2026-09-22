"""
fold_effect.py — draws the desktop folding shut toward the laptop hinge.

Pure Cairo, no GTK, so it can be rendered straight to an image and checked
by eye or by test. The daemon in toby_fold.py owns the window and the
timing; this file only answers "what does frame p look like".

How the fold is built
---------------------
The display is treated as a flat panel hinged along its bottom edge, the way
a laptop lid is. Closing it rotates the panel about that edge, so a row of
pixels at height t (0 at the hinge, 1 at the top) moves down to
``t * cos(angle)`` and toward the viewer by ``t * sin(angle)``. Perspective
makes rows that come toward the viewer draw wider. Cairo only does affine
transforms, so true perspective is approximated the way it has been in 2D
engines for decades: the picture is cut into thin horizontal bands and each
band is drawn with its own scale. With enough bands the steps disappear,
and because the source is pre-shrunk once before the animation starts, each
frame is a few dozen cheap image blits rather than a full-resolution warp.

Stages, all driven by one progress value p from 0 (open) to 1 (closed):

1. **Compress** — the whole desktop eases slightly toward the centre.
2. **Bend** — the panel starts rotating about the hinge; the top leans in.
3. **Fold** — rotation accelerates toward flat; the hinge shadow deepens.
4. **Black** — the last part of the motion fades to black.

Opening is exactly the same function run from p=1 back to p=0, which is
what makes the unfold read as the same motion in reverse rather than a
different animation.
"""

import math

import cairo

from toby_anim import clamp01, ease_in_cubic, ease_in_out_cubic, ease_out_cubic

# Number of horizontal bands the panel is cut into. More bands means smoother
# perspective and more work per frame; 72 is past the point where individual
# steps are visible on a 1080p screen in motion.
BANDS = 72

# The source is shrunk to at most this width once, before the animation.
# The picture is moving, darkening and blurring the whole time, so full
# resolution would cost several times more per frame for no visible gain.
MAX_SOURCE_WIDTH = 1280


def prepare_source(surface_or_path, max_width=MAX_SOURCE_WIDTH):
    """Load (or take) a screenshot and shrink it once for animating."""
    if isinstance(surface_or_path, cairo.ImageSurface):
        source = surface_or_path
    else:
        source = cairo.ImageSurface.create_from_png(str(surface_or_path))
    width, height = source.get_width(), source.get_height()
    if width <= max_width:
        return source
    scale = max_width / width
    new_w, new_h = max(1, int(width * scale)), max(1, int(height * scale))
    small = cairo.ImageSurface(cairo.FORMAT_ARGB32, new_w, new_h)
    cr = cairo.Context(small)
    cr.scale(scale, scale)
    cr.set_source_surface(source, 0, 0)
    cr.get_source().set_filter(cairo.FILTER_GOOD)
    cr.paint()
    small.flush()
    return small


def fold_params(settings):
    """Pull just the fold knobs out of the animation settings."""
    return {
        "strength": settings.get("fold_strength", 1.0),
        "perspective": settings.get("fold_perspective", 1.0),
        "zoom": settings.get("fold_zoom", 0.07),
        "blur": settings.get("fold_blur", 1.0),
        "lighting": settings.get("fold_lighting", 1.0),
    }


def _stages(p, params):
    """Split one progress value into the eased amount for each stage."""
    p = clamp01(p)
    compress = ease_out_cubic(clamp01(p / 0.4))
    fold = ease_in_out_cubic(clamp01((p - 0.12) / 0.8))
    black = ease_in_cubic(clamp01((p - 0.7) / 0.3))
    angle = fold * (math.pi / 2) * clamp01(params.get("strength", 1.0))
    return compress, angle, black


def band_geometry(p, width, height, params):
    """Where each band of the source lands on screen at progress p.

    Returns (scale, list of (src_y0, src_y1, dst_y0, dst_y1, dst_width)).
    Exposed separately so tests can check the geometry — bands stay in
    order, nothing escapes the screen, the hinge stays put — without having
    to inspect pixels.
    """
    compress, angle, _black = _stages(p, params)
    zoom = 1.0 - params.get("zoom", 0.07) * compress
    # How strongly rows coming toward the viewer grow. Capped so the top of
    # the panel never balloons past the edges of the screen.
    k = min(0.35, 0.22 * params.get("perspective", 1.0))
    sin_a, cos_a = math.sin(angle), math.cos(angle)

    cx = width / 2.0
    hinge_y = height - (height * (1.0 - zoom)) / 2.0  # zoom shrinks about the centre
    panel_h = height * zoom

    bands = []
    for i in range(BANDS):
        # t measured from the hinge: band 0 is the top of the screen
        src_y0 = height * i / BANDS
        src_y1 = height * (i + 1) / BANDS
        t0 = 1.0 - i / BANDS
        t1 = 1.0 - (i + 1) / BANDS

        def project(t):
            grow = 1.0 / max(0.2, 1.0 - k * t * sin_a)
            # Perspective widens rows fully but lifts them only partly, so
            # the panel keeps reading as folding *down* toward the hinge
            # rather than swinging up toward the viewer.
            return hinge_y - t * panel_h * cos_a * math.sqrt(grow), grow

        y0, g0 = project(t0)
        y1, g1 = project(t1)
        grow = (g0 + g1) / 2.0
        # never wider than the screen itself
        dst_w = min(width, width * zoom * grow)
        bands.append((src_y0, src_y1, y0, y1, dst_w))
    return cx, bands


def render_fold(cr, width, height, source, p, params, previous_p=None):
    """Draw frame p of the fold onto cr, which covers width x height.

    source is a surface from prepare_source(). previous_p, if given, is the
    progress one frame earlier; it drives the motion blur.
    """
    p = clamp01(p)
    _compress, angle, black = _stages(p, params)

    # Everything the panel uncovers is the dark beneath the lid.
    cr.set_source_rgb(0, 0, 0)
    cr.paint()

    if black >= 0.999:
        return

    _draw_panel(cr, width, height, source, p, params, alpha=1.0)

    blur = params.get("blur", 1.0)
    if blur > 0 and previous_p is not None and abs(previous_p - p) > 0.002:
        _draw_motion_trail(cr, width, height, p, previous_p, params, blur)

    _draw_lighting(cr, width, height, p, angle, params)

    if black > 0:
        cr.set_source_rgba(0, 0, 0, black)
        cr.paint()


def _draw_panel(cr, width, height, source, p, params, alpha):
    cx, bands = band_geometry(p, width, height, params)
    src_w, src_h = source.get_width(), source.get_height()
    sx = src_w / float(width)
    sy = src_h / float(height)

    pattern = cairo.SurfacePattern(source)
    pattern.set_filter(cairo.FILTER_BILINEAR)

    for src_y0, src_y1, dst_y0, dst_y1, dst_w in bands:
        dst_h = dst_y1 - dst_y0
        if dst_h <= 0.05:
            continue  # edge-on; nothing to see
        band_h = src_y1 - src_y0
        cr.save()
        # overlap each band by a pixel so rounding never opens a seam
        cr.rectangle(cx - dst_w / 2.0, dst_y0 - 0.6, dst_w, dst_h + 1.2)
        cr.clip()
        # map this band of the (shrunk) source onto its destination rectangle
        scale_x = dst_w / float(width)
        scale_y = dst_h / band_h
        # destination -> source mapping, which is what a pattern matrix holds
        matrix = cairo.Matrix(
            xx=sx / scale_x, yx=0.0,
            xy=0.0, yy=sy / scale_y,
            x0=-(cx - dst_w / 2.0) * sx / scale_x,
            y0=src_y0 * sy - dst_y0 * sy / scale_y,
        )
        pattern.set_matrix(matrix)
        cr.set_source(pattern)
        if alpha >= 0.999:
            cr.paint()
        else:
            cr.paint_with_alpha(alpha)
        cr.restore()


def _draw_motion_trail(cr, width, height, p, previous_p, params, blur):
    """A soft streak behind the panel's leading (top) edge.

    Real motion blur would mean drawing the panel several times per frame.
    Almost all of the visible blur in a fold is at the top edge, though,
    because that is the part moving fastest — so a gradient smear between
    where the edge was last frame and where it is now sells the motion for
    the cost of one filled shape. (A second full copy of the panel was tried
    first; it read as a glitch, a crisp detached strip, not as blur.)
    """
    _cx, now_bands = band_geometry(p, width, height, params)
    _cx, then_bands = band_geometry(previous_p, width, height, params)
    now_top, then_top = now_bands[0][2], then_bands[0][2]
    if abs(now_top - then_top) < 1.0:
        return
    cx = width / 2.0
    w_now, w_then = now_bands[0][4], then_bands[0][4]
    lead, trail = (now_top, then_top)
    grad = cairo.LinearGradient(0, lead, 0, trail)
    strength = min(0.55, 0.35 * blur)
    grad.add_color_stop_rgba(0, 0.12, 0.13, 0.22, strength)
    grad.add_color_stop_rgba(1, 0.12, 0.13, 0.22, 0.0)
    cr.move_to(cx - w_now / 2, lead)
    cr.line_to(cx + w_now / 2, lead)
    cr.line_to(cx + w_then / 2, trail)
    cr.line_to(cx - w_then / 2, trail)
    cr.close_path()
    cr.set_source(grad)
    cr.fill()


def _draw_lighting(cr, width, height, p, angle, params):
    lighting = params.get("lighting", 1.0)
    if lighting <= 0 or angle <= 0.001:
        return
    cx, bands = band_geometry(p, width, height, params)
    top = bands[0][2]
    bottom = bands[-1][3]
    if bottom - top < 1:
        return
    fold = angle / (math.pi / 2)

    # The panel turns away from the room's light as it closes: darker
    # overall, darkest at the far edge.
    shade = cairo.LinearGradient(0, top, 0, bottom)
    shade.add_color_stop_rgba(0.0, 0, 0, 0, min(0.85, lighting * 0.62 * fold))
    shade.add_color_stop_rgba(1.0, 0, 0, 0, min(0.85, lighting * 0.22 * fold))
    top_w = bands[0][4]
    bottom_w = bands[-1][4]
    cr.move_to(cx - top_w / 2, top)
    cr.line_to(cx + top_w / 2, top)
    cr.line_to(cx + bottom_w / 2, bottom)
    cr.line_to(cx - bottom_w / 2, bottom)
    cr.close_path()
    cr.set_source(shade)
    cr.fill()

    # A thin glint along the top edge, catching the light as it tips toward
    # you — the detail that makes it read as a physical panel.
    glint = min(0.5, lighting * 0.5 * math.sin(angle * 2))
    if glint > 0.01:
        cr.set_line_width(max(1.0, 2.5 * lighting))
        cr.set_source_rgba(1, 1, 1, glint)
        cr.move_to(cx - top_w / 2, top)
        cr.line_to(cx + top_w / 2, top)
        cr.stroke()

    # A soft contact shadow where the panel meets the hinge.
    hinge = cairo.LinearGradient(0, bottom - 60, 0, bottom)
    hinge.add_color_stop_rgba(0, 0, 0, 0, 0)
    hinge.add_color_stop_rgba(1, 0, 0, 0, min(0.7, lighting * 0.5 * fold))
    cr.rectangle(cx - bottom_w / 2, bottom - 60, bottom_w, 60)
    cr.set_source(hinge)
    cr.fill()


def render_frame_to_png(source, width, height, p, params, path, previous_p=None):
    """Render one frame to a PNG — for tests and for the preview command."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)
    render_fold(cr, width, height, source, p, params, previous_p)
    surface.flush()
    surface.write_to_png(str(path))
