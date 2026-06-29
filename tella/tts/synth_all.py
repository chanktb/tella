"""Synthesize ONE continuous narration MP3 for the whole plan.

The continuous-narration model (CEO 2026-06-29):
  * Join every body scene's ``voice_script`` into a single TTS input and
    issue ONE synthesis call. Edge TTS / Google TTS handle the inter-
    sentence breath pauses naturally inside the utterance — far smoother
    than concatenating N independently-synthesized MP3s, which each carry
    ~0.3-0.6 s of baked-in leading/trailing silence that compounded into
    1+ second gaps on scene boundaries.
  * Measure the resulting audio's total duration. Distribute it across
    scenes in proportion to each scene's ``voice_script`` character count
    — TTS speaks at a roughly constant chars/sec — so visual cuts still
    land near the right phrase. (Exact word-level alignment would need a
    forced aligner; char-proportion gets us within ~0.3 s, usually
    imperceptible against a Ken Burns image.)
  * The single audio file is recorded on the plan as
    ``narration_audio_filename``. Per-scene ``audio_filename`` is left
    blank — the render layer mixes the single track at final-mux time.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from tella.planner.models import TellaScenePlan
from tella.tts import edge, google

logger = logging.getLogger("tella.tts.synth_all")


async def _ffprobe_duration(path: Path) -> float:
    """Return audio duration in seconds via ffprobe."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: "
            f"{stderr.decode('utf-8', errors='replace')[-200:]}"
        )
    try:
        return float(stdout.decode("ascii").strip() or "0")
    except ValueError:
        return 0.0


def _join_voice_scripts(scenes) -> str:
    """Concatenate per-scene voice_script into one TTS input.

    Joins with a space — Edge/Google TTS treat sentence-end punctuation as
    a natural breath cue, so we don't need to force extra padding. If a
    scene's script doesn't end in punctuation, we add a period so the TTS
    engine inflects it as a sentence end.
    """
    parts: list[str] = []
    for s in scenes:
        text = (s.voice_script or "").strip()
        if not text:
            continue
        # Ensure each scene ends with terminal punctuation so the TTS
        # engine plays a natural beat between them.
        if text[-1] not in ".!?…":
            text = text + "."
        parts.append(text)
    return " ".join(parts)


def _distribute_durations(scenes, total_duration: float) -> None:
    """Set ``scene.audio_duration`` for each scene by char-proportion.

    Rounding errors are absorbed into the final scene so
    ``sum(scene.audio_duration) == total_duration`` exactly (to 2 d.p.).
    """
    chars = [max(1, len((s.voice_script or "").strip())) for s in scenes]
    total_chars = sum(chars)
    if total_chars <= 0 or total_duration <= 0:
        # Defensive fallback — distribute evenly.
        even = total_duration / max(1, len(scenes))
        for s in scenes:
            s.audio_duration = round(even, 2)
        return

    running = 0.0
    for i, scene in enumerate(scenes):
        if i == len(scenes) - 1:
            scene.audio_duration = round(total_duration - running, 2)
        else:
            d = round(total_duration * chars[i] / total_chars, 2)
            scene.audio_duration = d
            running = round(running + d, 2)


async def synthesize_all(
    plan: TellaScenePlan,
    job_dir: Path,
    *,
    google_tts_api_key: str = "",
    google_tts_voice: str = "",
) -> None:
    """Synthesize the narration as ONE continuous MP3.

    Mutates the plan in place:
      * ``plan.narration_audio_filename`` → ``"assets/narration.mp3"``
      * ``scene.audio_duration`` set by char-proportional split
      * ``scene.audio_filename`` left blank (render mixes the single track)

    Voice provider priority:
      1. Google Cloud TTS Chirp 3 HD (when key + voice set, kill switch off)
      2. Edge TTS — always-on fallback

    Raises:
        RuntimeError: when both providers fail.
    """
    job_dir = Path(job_dir)
    assets_dir = job_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    body_scenes = [s for s in plan.scenes if s.kind == "scene"]
    if not body_scenes:
        return

    full_text = _join_voice_scripts(body_scenes)
    if not full_text.strip():
        return

    out = assets_dir / "narration.mp3"
    used_provider = "edge"
    google_enabled = bool(google_tts_api_key) and bool(google_tts_voice)

    if google_enabled and not google.is_dead():
        ok = await google.synth_google(
            text=full_text,
            voice_name=google_tts_voice,
            rate=plan.voice_edge_rate,
            api_key=google_tts_api_key,
            out_path=out,
        )
        if ok:
            used_provider = "google"

    if used_provider == "edge":
        await edge.synthesize(
            full_text,
            plan.voice_name,
            out,
            rate=plan.voice_edge_rate,
        )

    total_duration = await _ffprobe_duration(out)
    _distribute_durations(body_scenes, total_duration)

    plan.narration_audio_filename = f"assets/{out.name}"
    # Clear any stale per-scene audio_filename from older runs of this plan.
    for s in body_scenes:
        s.audio_filename = ""

    logger.info(
        "synthesize_all: 1 combined narration (%.2fs, provider=%s, voice=%s @ %s) "
        "distributed across %d scenes",
        total_duration,
        f"google:{google_tts_voice}" if used_provider == "google" else "edge",
        plan.voice_name, plan.voice_edge_rate,
        len(body_scenes),
    )


__all__ = ["synthesize_all"]
