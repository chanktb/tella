"""Render title + caption to a transparent RGBA PNG using Pillow.

Why this exists: the ffmpeg-static binary on production VPS ships
WITHOUT the ``drawtext`` filter compiled in (despite
``--enable-libfreetype`` in the configure string). The Lingora render
chain works around it by overlaying a pre-rendered PNG watermark
through the ``overlay`` filter. Tella does the same for its title +
caption layers.

This module produces one transparent PNG per scene; the render
pipeline then composites it on top of the Ken Burns / clip background
via ``overlay=0:0``.

Layout (matches the safe zone constants in
:mod:`tella.composer.safe_zone`):

  - Title : rendered top, anchored at ``safe.top + TITLE_TOP_PADDING``
  - Caption: rendered bottom, anchored ``safe.bottom - CAPTION_BOTTOM_PADDING``
  - Both boxed in semi-transparent black with `TEXT_BOX_PADDING` margin
  - Both wrapped to fit the safe-zone width
"""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("tella.render.text_overlay")

# Mirror the constants from pipeline.py (kept in sync so behaviour matches
# whether drawtext is available or not).
TITLE_FONT_SIZE = 60
CAPTION_FONT_SIZE = 42
BRAND_FONT_SIZE = 36
TITLE_MAX_LINES = 2
CAPTION_MAX_LINES = 4
TEXT_BOX_PADDING = 22
BRAND_BOX_PADDING = 14
TEXT_BOX_OPACITY = 0.55
BRAND_BOX_OPACITY = 0.45
CAPTION_BOTTOM_PADDING = 60
TITLE_TOP_PADDING = 50
BRAND_TOP_PADDING = 24
LINE_SPACING = 8


def _measure(font: ImageFont.FreeTypeFont, text: str) -> tuple[int, int]:
    """Return (width, ascent+descent) for ``text`` at ``font``."""
    # bbox: (left, top, right, bottom) of ink
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _wrap_pixel(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width_px: int,
    max_lines: int,
) -> list[str]:
    """Pixel-accurate word wrap. Falls back to char-by-char for CJK."""
    text = (text or "").strip()
    if not text:
        return []
    words = text.split()
    if any(_measure(font, w)[0] > max_width_px for w in words):
        # A single token is too long (likely CJK with no spaces) — wrap by char.
        chars = list(text)
        lines: list[str] = []
        cur = ""
        for ch in chars:
            cand = cur + ch
            if _measure(font, cand)[0] <= max_width_px:
                cur = cand
            else:
                if cur:
                    lines.append(cur)
                cur = ch
                if len(lines) >= max_lines:
                    break
        if cur and len(lines) < max_lines:
            lines.append(cur)
        return lines

    lines: list[str] = []
    cur = ""
    for w in words:
        cand = (cur + " " + w).strip() if cur else w
        if _measure(font, cand)[0] <= max_width_px:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)

    if len(lines) >= max_lines:
        # Mark truncation on the last line with an ellipsis if there's
        # still text left (poor approximation but signals "more").
        last = lines[-1]
        if not last.endswith("…") and len(last) > 4:
            lines[-1] = last[:-1].rstrip() + "…"
    return lines


def _draw_text_box(
    draw: ImageDraw.ImageDraw,
    *,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    canvas_w: int,
    top_y: int,
    pad: int,
    opacity: float,
    line_spacing: int,
) -> int:
    """Draw a centered text block with a semi-transparent dark box.

    Returns the bottom y-coordinate of the rendered block (after the box).
    """
    if not lines:
        return top_y

    # Measure block dimensions.
    line_heights = []
    line_widths = []
    for line in lines:
        w, h = _measure(font, line)
        line_widths.append(w)
        line_heights.append(h)
    block_w = max(line_widths)
    block_h = sum(line_heights) + line_spacing * max(0, len(lines) - 1)

    box_w = block_w + 2 * pad
    box_h = block_h + 2 * pad
    box_x = (canvas_w - box_w) // 2
    box_y = top_y

    alpha = max(0, min(255, int(opacity * 255)))
    draw.rectangle(
        (box_x, box_y, box_x + box_w, box_y + box_h),
        fill=(0, 0, 0, alpha),
    )

    cur_y = box_y + pad
    for line, w, h in zip(lines, line_widths, line_heights, strict=True):
        x = (canvas_w - w) // 2
        draw.text((x, cur_y), line, font=font, fill=(255, 255, 255, 255))
        cur_y += h + line_spacing
    return box_y + box_h


def _circular_avatar(path: str, size: int) -> Image.Image | None:
    """Load an avatar image and crop it to a circle of ``size`` px. None on error."""
    try:
        with Image.open(path) as im:
            im = im.convert("RGBA")
            # Center-crop to square first.
            w, h = im.size
            side = min(w, h)
            im = im.crop(((w - side) // 2, (h - side) // 2,
                          (w - side) // 2 + side, (h - side) // 2 + side))
            im = im.resize((size, size), Image.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
            im.putalpha(mask)
            return im
    except (OSError, ValueError) as exc:
        logger.warning("avatar load failed (%s): %s", path, exc)
        return None


def _draw_brand_row(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    *,
    text: str,
    font: ImageFont.FreeTypeFont,
    safe_left: int,
    top_y: int,
    avatar_path: str | None = None,
) -> int:
    """Draw a left-aligned brand pill: optional circular avatar + channel
    name (name only — no handle/slug). Returns the pill's bottom y."""
    # Use the ink bbox (not just width/height): PIL anchors text at the em-box
    # origin, which includes empty space above the glyphs, so drawing at
    # box_y+pad would push the text visually low. We subtract the bbox offset
    # (l, t) so the actual ink is centered with equal padding top and bottom.
    left, top, right, bottom = font.getbbox(text)
    tw = right - left
    th = bottom - top
    pad = BRAND_BOX_PADDING
    avatar_size = th + 2 * pad - 8  # avatar fills the pill height minus a hair
    avatar = _circular_avatar(avatar_path, avatar_size) if avatar_path else None
    gap = 12 if avatar else 0

    box_x = safe_left
    box_y = top_y
    box_w = pad + (avatar_size + gap if avatar else 0) + tw + pad
    box_h = th + 2 * pad
    alpha = max(0, min(255, int(BRAND_BOX_OPACITY * 255)))
    draw.rounded_rectangle(
        (box_x, box_y, box_x + box_w, box_y + box_h),
        radius=box_h // 2, fill=(0, 0, 0, alpha),
    )

    cur_x = box_x + pad
    if avatar:
        canvas.paste(avatar, (cur_x, box_y + (box_h - avatar_size) // 2), avatar)
        cur_x += avatar_size + gap
    # Offset by (left, top) so the glyph ink lands exactly at the padded box
    # interior — equal gap above and below.
    draw.text((cur_x - left, box_y + pad - top), text, font=font, fill=(255, 255, 255, 240))
    return box_y + box_h


def render_overlay_png(
    *,
    title: str | None,
    caption: str | None,
    canvas_w: int,
    canvas_h: int,
    safe_top: int,
    safe_bottom: int,
    safe_left: int,
    safe_right: int,
    font_file: Path,
    out_path: Path,
    channel_name: str | None = None,
    channel_avatar: str | None = None,
) -> Path | None:
    """Render an RGBA PNG with an optional channel brand row at the very top,
    the scene title below it, and the caption at the bottom (all inside the
    safe zone). The brand row shows the channel NAME (no handle/slug) and,
    if provided, a circular avatar image to its left.

    Returns ``out_path`` on success, or ``None`` when there is nothing to
    draw (caller can skip the overlay filter entirely).
    """
    if not title and not caption and not channel_name:
        return None

    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    safe_w = safe_right - safe_left
    # Effective wrap width = safe zone minus the box padding on both sides.
    wrap_w = max(40, safe_w - 2 * TEXT_BOX_PADDING)

    # ── Channel brand row (top), if any ────────────────────────────────
    title_top = safe_top + TITLE_TOP_PADDING
    if channel_name:
        brand_font = ImageFont.truetype(str(font_file), BRAND_FONT_SIZE)
        brand_bottom = _draw_brand_row(
            canvas,
            draw,
            text=channel_name.strip(),
            font=brand_font,
            safe_left=safe_left,
            top_y=safe_top + BRAND_TOP_PADDING,
            avatar_path=channel_avatar,
        )
        # Push the title below the brand row so they never collide.
        title_top = max(title_top, brand_bottom + BRAND_TOP_PADDING)

    if title:
        title_font = ImageFont.truetype(str(font_file), TITLE_FONT_SIZE)
        title_lines = _wrap_pixel(title, title_font, wrap_w, TITLE_MAX_LINES)
        if title_lines:
            _draw_text_box(
                draw,
                lines=title_lines,
                font=title_font,
                canvas_w=canvas_w,
                top_y=title_top,
                pad=TEXT_BOX_PADDING,
                opacity=TEXT_BOX_OPACITY,
                line_spacing=LINE_SPACING,
            )

    if caption:
        cap_font = ImageFont.truetype(str(font_file), CAPTION_FONT_SIZE)
        cap_lines = _wrap_pixel(caption, cap_font, wrap_w, CAPTION_MAX_LINES)
        if cap_lines:
            # Measure block height to anchor the box at the bottom of the
            # safe zone.
            heights = [_measure(cap_font, line)[1] for line in cap_lines]
            block_h = sum(heights) + LINE_SPACING * max(0, len(cap_lines) - 1)
            box_h = block_h + 2 * TEXT_BOX_PADDING
            top_y = safe_bottom - CAPTION_BOTTOM_PADDING - box_h
            _draw_text_box(
                draw,
                lines=cap_lines,
                font=cap_font,
                canvas_w=canvas_w,
                top_y=max(safe_top, top_y),
                pad=TEXT_BOX_PADDING,
                opacity=TEXT_BOX_OPACITY,
                line_spacing=LINE_SPACING,
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "PNG")
    logger.info("overlay PNG saved %s (%d KB)", out_path.name, out_path.stat().st_size // 1024)
    return out_path


__all__ = [
    "render_overlay_png",
]
