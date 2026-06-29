"""Compose timing for every scene in a plan.

After Phase 3 (media) + Phase 4 (TTS) populate ``image_filenames`` and per-
scene ``audio_duration``, ``compose_timing`` walks the scenes and assigns
``start`` + ``duration`` per scene + sets the plan's ``total_duration``.

Continuous-narration model (CEO 2026-06-29): there is ONE narration audio
track (``plan.narration_audio_filename``) covering the whole video. Each
scene's ``audio_duration`` is a slice of that track set by
:func:`tella.tts.synth_all.synthesize_all` via char-proportional split.
The render layer plays the single audio over the concatenated video-only
scenes, so we do NOT add per-scene tail buffers — that would desynchronize
visuals from the continuous audio.
"""
from __future__ import annotations

import logging

from tella.planner.models import TellaScenePlan

logger = logging.getLogger("tella.composer.compose")


def compose_timing(plan: TellaScenePlan) -> TellaScenePlan:
    """Cursor-walk the scenes; set ``start`` + ``duration`` + plan total.

    Mutates the plan in place. Returns the same plan for chaining.

    Pre-requirement: every body scene already has ``audio_duration``
    populated (by :func:`tella.tts.synth_all.synthesize_all`).
    """
    body_scenes = [s for s in plan.scenes if s.kind == "scene"]
    if not body_scenes:
        plan.total_duration = 0.0
        return plan

    cursor = 0.0
    for scene in body_scenes:
        if scene.audio_duration <= 0:
            logger.warning(
                "scene %d audio_duration=0 — did TTS run? falling back to 6s",
                scene.scene_index,
            )
            scene.audio_duration = 6.0
        # Visual duration == audio slice for this scene. No tail buffer —
        # the continuous narration must not be interrupted by silent visual
        # padding between scenes.
        scene.duration = round(scene.audio_duration, 2)
        scene.start = round(cursor, 2)
        cursor = round(cursor + scene.duration, 2)

    plan.total_duration = round(cursor, 2)
    logger.info(
        "compose_timing: %d scenes, total=%.2fs",
        len(body_scenes), plan.total_duration,
    )
    return plan


__all__ = ["compose_timing"]
