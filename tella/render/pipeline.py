"""ffmpeg render pipeline — turn a fully-composed plan into a final MP4.

Pipeline (continuous-narration model, CEO 2026-06-29):
  1. Per scene: assemble VIDEO-ONLY background + title + caption → scene_NN.mp4
     (no audio map — keeps scene MP4s aligned strictly to visual timing)
  2. Concatenate all scene MP4s → silent_video.mp4
  3. Mix ``plan.narration_audio_filename`` onto the silent video → video.mp4

Why one audio track:
  Per-scene TTS files each carry ~0.3-0.6 s of leading + trailing silence
  baked in by the synth engine. Concatenating N of them stacks ~1 s of
  dead air on every scene boundary — fine for 8-scene videos, severe
  monotony-breakage on 30-scene ones. A single TTS call produces one
  utterance with natural inter-sentence breath pauses (much shorter and
  rhythmically correct).

Background handling per media_source:
  - ai_image / stock_photo : Ken Burns zoompan over the still image
  - stock_video            : scale + crop the clip to canvas, loop if shorter

Text overlays (all positioned inside the safe zone):
  - Title  : top, larger font, scene title in TARGET_LANG
  - Caption: bottom, smaller font, narration text wrapped to fit width

Concat: simple cut between scenes (no crossfade in v1; xfade can land
in a refinement session).

Font: Windows-friendly default (Arial). Override via env
``TELLA_FONT_FILE`` if you have something prettier on PATH.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
from pathlib import Path

from tella.composer.compose import compose_timing
from tella.composer.safe_zone import render_dims_for, safe_zone_for
from tella.composer.text_wrap import chars_per_line, wrap
from tella.planner.models import TellaScenePlan
from tella.render.text_overlay import render_overlay_png

logger = logging.getLogger("tella.render.pipeline")

OUTPUT_FPS = 30
TITLE_FONT_SIZE = 60
CAPTION_FONT_SIZE = 42
TITLE_MAX_LINES = 2
CAPTION_MAX_LINES = 4
TEXT_BOX_PADDING = 22
TEXT_BOX_OPACITY = 0.55

# Bottom band reserved for caption — top of caption sits this far above
# the safe-zone bottom edge so multi-line captions still fit.
CAPTION_BOTTOM_PADDING = 60
TITLE_TOP_PADDING = 50


def _resolve_font_file() -> Path:
    """Find a TTF/OTF font ffmpeg can use. Prefers env override; then
    common Windows/macOS/Linux system paths."""
    env = (os.environ.get("TELLA_FONT_FILE") or "").strip()
    if env:
        p = Path(env)
        if p.is_file():
            return p
        logger.warning("TELLA_FONT_FILE %s not found — falling back", env)

    candidates: list[str] = []
    if sys.platform == "win32":
        windir = os.environ.get("WINDIR", "C:\\Windows")
        candidates = [
            f"{windir}\\Fonts\\arial.ttf",
            f"{windir}\\Fonts\\segoeui.ttf",
            f"{windir}\\Fonts\\calibri.ttf",
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Library/Fonts/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        ]
    else:  # linux — cover Debian/Ubuntu + RHEL/Rocky/Fedora layouts
        candidates = [
            # Debian / Ubuntu layout
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            # RHEL / Rocky / Fedora layout (no truetype/ prefix)
            "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf",
            "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
            # Google Noto variable fonts (common on modern Rocky/RHEL)
            "/usr/share/fonts/google-noto-vf/NotoSans[wght].ttf",
            "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        ]
    for c in candidates:
        if Path(c).is_file():
            return Path(c)
    raise RuntimeError(
        "no usable TTF font found on this OS. Set TELLA_FONT_FILE in .env"
    )


def _ffmpeg_path_escape(p: Path) -> str:
    """Escape a path for use INSIDE an ffmpeg filter_complex string.

    ffmpeg filter syntax treats ``:`` `,`` `'`` `\\` as metacharacters.
    Forward slashes are always safe, even on Windows.
    """
    s = str(p).replace("\\", "/")
    # On Windows, drive letter like 'C:/...' needs the colon escaped
    # because drawtext options use ':' as separator.
    s = s.replace(":", "\\:")
    return s


def _is_video_asset(path: Path) -> bool:
    return path.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv")


def _write_text_file(path: Path, text: str) -> Path:
    """Write text to a temp file ffmpeg's drawtext ``textfile=`` can read.

    drawtext on some ffmpeg builds chokes on BOM — we write plain UTF-8.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _build_bg_filter(
    *,
    is_video: bool,
    canvas_w: int,
    canvas_h: int,
    duration: float,
    ken_burns_max_scale: float,
) -> str:
    """Background-only filter chain (scale+crop+Ken Burns / fps).

    Text overlays are rendered via PNG composite (Pillow + ffmpeg
    ``overlay`` filter) because ffmpeg-static on prod VPS lacks the
    ``drawtext`` filter despite ``--enable-libfreetype`` in the configure
    string. See :mod:`tella.render.text_overlay`.
    """
    chains: list[str] = []
    chains.append(
        f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=increase,"
        f"crop={canvas_w}:{canvas_h}"
    )
    if not is_video:
        total_frames = max(2, int(duration * OUTPUT_FPS))
        zoom_step = (ken_burns_max_scale - 1.0) / max(1, total_frames - 1)
        chains.append(
            f"zoompan=z='min(zoom+{zoom_step:.6f},{ken_burns_max_scale})':"
            f"d={total_frames}:s={canvas_w}x{canvas_h}:fps={OUTPUT_FPS}"
        )
    else:
        chains.append(f"fps={OUTPUT_FPS}")
    return ",".join(chains)


async def _render_scene(
    *,
    asset_path: Path,
    out_path: Path,
    duration: float,
    canvas_w: int,
    canvas_h: int,
    safe_top: int,
    safe_bottom: int,
    safe_left: int,
    safe_right: int,
    title_text: str | None,
    caption_text: str | None,
    work_dir: Path,
    font_file: Path,
    scene_index: int,
    ken_burns_max_scale: float,
    channel_name: str | None = None,
    channel_avatar: str | None = None,
) -> Path:
    """Render one VIDEO-ONLY scene MP4. Audio is mixed in once at final-mux."""
    is_video = _is_video_asset(asset_path)

    # Pre-render the text overlay as a transparent PNG (Pillow). This
    # sidesteps ffmpeg's drawtext filter which isn't compiled into
    # ffmpeg-static on production VPS.
    overlay_png: Path | None = None
    if title_text or caption_text or channel_name:
        overlay_png = render_overlay_png(
            title=title_text,
            caption=caption_text,
            canvas_w=canvas_w,
            canvas_h=canvas_h,
            safe_top=safe_top,
            safe_bottom=safe_bottom,
            safe_left=safe_left,
            safe_right=safe_right,
            font_file=font_file,
            out_path=work_dir / f"scene_{scene_index:02d}_overlay.png",
            channel_name=channel_name,
            channel_avatar=channel_avatar,
        )

    bg_filter = _build_bg_filter(
        is_video=is_video,
        canvas_w=canvas_w,
        canvas_h=canvas_h,
        duration=duration,
        ken_burns_max_scale=ken_burns_max_scale,
    )

    cmd: list[str] = ["ffmpeg", "-y", "-loglevel", "error"]
    if is_video:
        cmd += ["-stream_loop", "-1", "-i", str(asset_path)]
    else:
        cmd += ["-loop", "1", "-i", str(asset_path)]

    if overlay_png is not None:
        # 2nd input: the transparent PNG. ffmpeg overlay composites it
        # on top of the bg chain.
        cmd += ["-i", str(overlay_png)]
        filter_complex = f"[0:v]{bg_filter}[bg];[bg][1:v]overlay=0:0[v]"
    else:
        filter_complex = f"[0:v]{bg_filter}[v]"

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-t", f"{duration:.3f}",
        "-r", str(OUTPUT_FPS),
        "-c:v", "libx264",
        "-preset", "veryfast",
        # CRF 26 is well within the "visually transparent on mobile" band
        # (22-28). The H.264 stream stays ~40% smaller than CRF 22 with
        # no perceptible quality loss on a phone screen — TikTok/Reels
        # re-compress anyway.
        "-crf", "26",
        "-pix_fmt", "yuv420p",
        "-an",                       # video-only scene
        "-movflags", "+faststart",
        str(out_path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"ffmpeg render scene {scene_index} failed:\n{msg[-1500:]}"
        )
    return out_path


async def _concat_scenes(
    scene_mp4s: list[Path], out_path: Path, work_dir: Path,
) -> Path:
    """Concatenate VIDEO-ONLY scene MP4s via the concat demuxer (codec copy).

    The concat demuxer resolves each ``file`` line RELATIVE TO THE LIST
    FILE itself — not the cwd. Since our list lives next to the scene
    MP4s, use bare filenames instead of paths.
    """
    list_file = work_dir / "concat.txt"
    list_file.write_text(
        "\n".join(
            f"file '{p.name}'"
            for p in scene_mp4s
        ) + "\n",
        encoding="utf-8",
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-an",                       # explicit video-only output
        "-movflags", "+faststart",
        str(out_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"ffmpeg concat failed:\n{msg[-1500:]}")
    return out_path


async def _mux_audio(
    silent_video: Path, audio: Path, out_path: Path,
) -> Path:
    """Mix the continuous narration onto the concatenated silent video.

    Uses ``-shortest`` so any tiny mismatch (round-off between the audio's
    real duration and the sum of scene visual durations) ends cleanly
    without a trailing silent frame. Audio is encoded to AAC 96k mono —
    Edge/Google TTS output is mono speech.
    """
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(silent_video),
        "-i", str(audio),
        "-map", "0:v",
        "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "96k",
        "-ar", "44100",
        "-ac", "1",
        "-shortest",
        "-movflags", "+faststart",
        str(out_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"ffmpeg final mux failed:\n{msg[-1500:]}")
    return out_path


async def render(plan: TellaScenePlan, job_dir: Path) -> Path:
    """Render the final video MP4. Returns the path to ``<job_dir>/video.mp4``.

    Pre-requirements (in order):
      1. :func:`tella.media.fetch.fetch_assets` populated
         ``scene.image_filenames``.
      2. :func:`tella.tts.synth_all.synthesize_all` populated
         ``scene.audio_filename`` + ``audio_duration``.

    This function calls :func:`tella.composer.compose.compose_timing`
    internally to set ``start`` / ``duration`` / ``total_duration``.
    """
    job_dir = Path(job_dir)
    work_dir = job_dir / "_render"
    work_dir.mkdir(parents=True, exist_ok=True)

    canvas_w, canvas_h = render_dims_for(plan.aspect_ratio)
    sz = safe_zone_for(plan.aspect_ratio)
    font_file = _resolve_font_file()

    compose_timing(plan)

    # Pre-compute the caption/title char budget once.
    title_cpl = chars_per_line(sz.width, TITLE_FONT_SIZE)
    caption_cpl = chars_per_line(sz.width, CAPTION_FONT_SIZE)

    # Pull the theme's Ken Burns end scale for image-mode scenes.
    from tella.themes.loader import load_theme

    theme_spec = load_theme(plan.theme)
    ken_burns_max_scale = max(1.01, float(theme_spec.ken_burns.end_scale))

    # Channel brand row — shown on every scene unless demo mode / blank.
    # Name only (no handle/slug) plus an optional circular avatar.
    brand_name = (plan.channel_name or "").strip() if not plan.demo_mode else ""
    brand_avatar = (plan.channel_avatar or "").strip() if not plan.demo_mode else ""
    if brand_name:
        logger.info("brand row: %r avatar=%s", brand_name, bool(brand_avatar))

    body_scenes = [s for s in plan.scenes if s.kind == "scene"]
    scene_mp4s: list[Path] = []

    logger.info(
        "render: %d scenes, canvas=%dx%d, total=%.2fs",
        len(body_scenes), canvas_w, canvas_h, plan.total_duration,
    )

    if not plan.narration_audio_filename:
        raise RuntimeError(
            "plan.narration_audio_filename empty — did synthesize_all run?"
        )

    for scene in body_scenes:
        if not scene.image_filenames:
            raise RuntimeError(
                f"scene {scene.scene_index}: no image_filenames "
                "(did fetch_assets run?)"
            )

        asset_path = job_dir / scene.image_filenames[0]
        out_mp4 = work_dir / f"scene_{scene.scene_index:02d}.mp4"

        title_lines = wrap(scene.title, title_cpl, max_lines=TITLE_MAX_LINES)
        caption_lines = wrap(scene.voice_script, caption_cpl, max_lines=CAPTION_MAX_LINES)
        title_text = "\n".join(title_lines) if title_lines else None
        caption_text = "\n".join(caption_lines) if caption_lines else None

        await _render_scene(
            asset_path=asset_path,
            out_path=out_mp4,
            duration=scene.duration,
            canvas_w=canvas_w,
            canvas_h=canvas_h,
            safe_top=sz.top,
            safe_bottom=sz.bottom,
            safe_left=sz.left,
            safe_right=sz.right,
            title_text=title_text,
            caption_text=caption_text,
            work_dir=work_dir,
            font_file=font_file,
            scene_index=scene.scene_index,
            ken_burns_max_scale=ken_burns_max_scale,
            channel_name=brand_name or None,
            channel_avatar=brand_avatar or None,
        )
        scene_mp4s.append(out_mp4)
        logger.info(
            "rendered scene %d/%d (%.2fs, video-only)",
            scene.scene_index, len(body_scenes), scene.duration,
        )

    # Stage 2: concat the video-only scenes → silent_video.mp4
    silent_video = work_dir / "silent_video.mp4"
    await _concat_scenes(scene_mp4s, silent_video, work_dir)
    logger.info("concat done → %s (silent)", silent_video.name)

    # Stage 3: mux the single continuous narration onto the silent video.
    final_path = job_dir / "video.mp4"
    narration_path = job_dir / plan.narration_audio_filename
    await _mux_audio(silent_video, narration_path, final_path)
    logger.info("render done → %s (%.2fs, continuous narration mixed)",
                final_path, plan.total_duration)
    return final_path


__all__ = [
    "OUTPUT_FPS",
    "render",
]
