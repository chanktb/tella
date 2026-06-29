"""Tella story planner — Gemini → :class:`TellaScenePlan`.

Public entry: :func:`plan_story`. Pass topic (already translated to
``target_lang``) + all the user-facing options. Returns a validated
plan with N scenes ready for the media + render layers.

The planner reuses the same retry pattern as story-teller's vcm/planner:
JSON loose-parse + Pydantic validation + up to MAX_ATTEMPTS retries with
repair prompts on validation failure.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from google import genai
from google.genai import types
from pydantic import ValidationError

from tella._gemini import (
    DEFAULT_MODEL_PLAN_DETAILED,
    DEFAULT_MODEL_PLAN_SHORT,
    get_client,
    is_transient_quota_error,
    parse_json_loose,
)
from tella._voice_pace import VoicePace, default_pace_for_theme
from tella.planner.character_lock import apply_lock
from tella.planner.models import (
    DurationMode,
    MediaSource,
    TellaScenePlan,
    Theme,
    VoiceGender,
)
from tella.planner.prompts import (
    build_system_prompt,
    build_user_prompt,
    build_user_script_system_prompt,
    build_user_script_user_prompt,
)
from tella.planner.voices import edge_voice_for
from tella.themes.loader import load_theme

logger = logging.getLogger("tella.planner.story_planner")

MAX_ATTEMPTS = 4


def _model_for(duration_mode: str) -> str:
    """Pick the right Gemini model for the duration mode (env overridable)."""
    if duration_mode == "detailed":
        return os.environ.get("GEMINI_MODEL_LONG") or DEFAULT_MODEL_PLAN_DETAILED
    return os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL_PLAN_SHORT


def _build_repair_prompt(topic: str, last_raw: str, err: Exception) -> str:
    return (
        "Your previous JSON failed validation. Fix the errors and output the "
        "FULL corrected JSON only. No prose, no markdown fence.\n\n"
        f"=== VALIDATION ERROR ===\n{err}\n\n"
        f"=== PREVIOUS OUTPUT (truncated to 2000 chars) ===\n{last_raw[:2000]}\n\n"
        f"=== ORIGINAL TOPIC ===\n{topic}\n"
    )


async def plan_story(
    *,
    topic: str,
    target_lang: str,
    aspect_ratio: str = "9:16",
    media_source: MediaSource = "ai_image",
    duration_mode: DurationMode = "short",
    theme: Theme = "cinematic",
    voice_pace: VoicePace | None = None,
    voice_gender: VoiceGender | None = None,
    client: genai.Client | None = None,
    model: str | None = None,
) -> TellaScenePlan:
    """Plan a story and return a validated :class:`TellaScenePlan`.

    Args:
        topic:          Topic ALREADY translated into ``target_lang`` (use
                        :func:`tella.ingest.topic_translator.translate_topic`).
        target_lang:    ISO-639-1, one of Tella's 8 supported langs.
        aspect_ratio:   ``"9:16"`` or ``"16:9"``.
        media_source:   ``"ai_image" | "stock_photo" | "stock_video"``.
        duration_mode:  ``"short"`` (5-8 scenes) or ``"detailed"`` (12-20).
        theme:          ``"parable" | "cinematic" | "playful" | "mindfulness"``.
        voice_pace:     :class:`VoicePace`. If None, resolved from theme.
        voice_gender:   ``"male" | "female"``. If None, resolved from theme.
        client:         Inject a pre-built ``genai.Client`` for tests.
        model:          Override Gemini model. Defaults from env / duration_mode.

    Returns:
        Validated :class:`TellaScenePlan` with character_lock applied
        (ai_image mode) and voice_name resolved (Edge TTS).

    Raises:
        ValueError: Topic empty or theme/media_source unknown.
        ValidationError: When Gemini fails to produce valid JSON after
            :data:`MAX_ATTEMPTS` retries.
    """
    topic_norm = (topic or "").strip()
    if not topic_norm:
        raise ValueError("topic is empty")

    theme_spec = load_theme(theme)  # raises FileNotFoundError if unknown

    # Resolve voice settings. Pace falls back to theme default; gender too.
    pace = voice_pace or default_pace_for_theme(theme)
    gender = (voice_gender or theme_spec.voice_gender_default).lower()
    voice_name = edge_voice_for(target_lang, gender)

    client = client or get_client()
    model = model or _model_for(duration_mode)

    system_prompt = build_system_prompt(
        theme=theme, duration_mode=duration_mode, media_source=media_source,
    )
    user_prompt = build_user_prompt(
        topic=topic_norm,
        target_lang=target_lang,
        aspect_ratio=aspect_ratio,
        media_source=media_source,
        duration_mode=duration_mode,
        theme=theme,
        voice_pace_name=pace.name,
        voice_edge_rate=pace.edge_rate,
        voice_google_rate=pace.google_rate,
        voice_gender=gender,
        voice_name=voice_name,
    )

    last_err: Exception | None = None
    last_raw = ""
    max_tokens = 16384 if duration_mode == "detailed" else 6144

    for attempt in range(1, MAX_ATTEMPTS + 1):
        temperature = 0.85 if attempt == 1 else 0.5
        try:
            resp = await asyncio.to_thread(
                client.models.generate_content,
                model=model,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                ),
            )
        except Exception as exc:
            last_err = exc
            transient = is_transient_quota_error(exc)
            logger.warning(
                "plan_story attempt %d Gemini error %s%s: %s",
                attempt, type(exc).__name__,
                " (transient — will rotate key + backoff)" if transient else "",
                exc,
            )
            if attempt < MAX_ATTEMPTS and transient:
                # Rebuild client → get_client() random-picks another key from
                # GEMINI_API_KEYS so the next attempt doesn't burn the same
                # quota. Small backoff so we don't tail-thrash the API.
                client = get_client()
                await asyncio.sleep(2 * attempt)
            continue

        last_raw = resp.text or ""
        try:
            data = parse_json_loose(last_raw)
        except json.JSONDecodeError as exc:
            last_err = exc
            logger.warning(
                "plan_story attempt %d JSON parse failed: %s — raw[:200]=%r",
                attempt, exc, last_raw[:200],
            )
            user_prompt = _build_repair_prompt(topic_norm, last_raw, exc)
            continue

        if not isinstance(data, dict):
            last_err = ValueError(f"expected JSON object, got {type(data).__name__}")
            continue

        # Defensive backfill: planner sometimes forgets the orchestration
        # echo fields. Inject ours so the schema validates.
        data.setdefault("title", topic_norm[:120])
        data["language"] = target_lang
        data["aspect_ratio"] = aspect_ratio
        data["media_source"] = media_source
        data["duration_mode"] = duration_mode
        data["theme"] = theme
        data["voice_pace_name"] = pace.name
        data["voice_edge_rate"] = pace.edge_rate
        data["voice_google_rate"] = pace.google_rate
        data["voice_gender"] = gender
        data["voice_name"] = voice_name

        # Stock modes: planner should null briefs, but enforce defensively.
        if media_source != "ai_image":
            data["characters"] = []
            data["character_brief"] = None
            data["setting_brief"] = None

        # Re-number scenes so composer can rely on 1..N ordering.
        scenes = data.get("scenes") or []
        for i, s in enumerate(scenes, start=1):
            if isinstance(s, dict):
                s["scene_index"] = i
                s["kind"] = s.get("kind") or "scene"

        try:
            plan = TellaScenePlan.model_validate(data)
        except ValidationError as exc:
            last_err = exc
            logger.warning("plan_story attempt %d validation failed: %s", attempt, exc)
            user_prompt = _build_repair_prompt(topic_norm, last_raw, exc)
            continue

        # Character lock: bake identity + setting + style suffix into each
        # scene's image_prompt (ai_image mode only).
        apply_lock(plan, style_suffix=theme_spec.image_style_suffix)

        if attempt > 1:
            logger.info(
                "plan_story succeeded on attempt %d/%d (%d scenes)",
                attempt, MAX_ATTEMPTS, len(plan.scenes),
            )
        else:
            logger.info("plan_story succeeded (%d scenes)", len(plan.scenes))
        return plan

    assert last_err is not None
    logger.error("plan_story gave up after %d attempts: %s", MAX_ATTEMPTS, last_err)
    raise last_err


async def plan_story_from_script(
    *,
    user_script: str,
    target_lang: str,
    aspect_ratio: str = "9:16",
    media_source: MediaSource = "ai_image",
    duration_mode: DurationMode = "short",
    theme: Theme = "cinematic",
    voice_pace: VoicePace | None = None,
    voice_gender: VoiceGender | None = None,
    client: genai.Client | None = None,
    model: str | None = None,
) -> TellaScenePlan:
    """Parse a user-supplied script into a validated :class:`TellaScenePlan`.

    Same return contract as :func:`plan_story` but the planner PARSES the
    user's script verbatim instead of generating from a topic. See
    ``_USER_SCRIPT_PARSE_RULES`` in prompts.py for the parse contract.

    CEO 2026-06-17: user pastes complete narration → Gemini splits into
    scenes (preserving exact wording in voice_script) + emits visuals
    matching each scene's content. The rest of the pipeline (media fetch,
    TTS, render) is identical to the topic mode.

    Args:
        user_script:    Full narration text in the target language. Must
                        not be empty. The model preserves this verbatim
                        across scene voice_scripts.
        target_lang:    ISO-639-1 of the script's language. We do NOT
                        translate the script — caller must pass the
                        actual language of the supplied text.
        (other args)    Same semantics as :func:`plan_story`.
    """
    script_norm = (user_script or "").strip()
    if not script_norm:
        raise ValueError("user_script is empty")
    if len(script_norm) < 30:
        raise ValueError(
            f"user_script too short ({len(script_norm)} chars). "
            "Paste a complete narration (minimum ~30 chars)."
        )

    theme_spec = load_theme(theme)
    pace = voice_pace or default_pace_for_theme(theme)
    gender = (voice_gender or theme_spec.voice_gender_default).lower()
    voice_name = edge_voice_for(target_lang, gender)

    client = client or get_client()
    model = model or _model_for(duration_mode)

    system_prompt = build_user_script_system_prompt(
        theme=theme, duration_mode=duration_mode, media_source=media_source,
    )
    user_prompt = build_user_script_user_prompt(
        user_script=script_norm,
        target_lang=target_lang,
        aspect_ratio=aspect_ratio,
        media_source=media_source,
        duration_mode=duration_mode,
        theme=theme,
        voice_pace_name=pace.name,
        voice_edge_rate=pace.edge_rate,
        voice_google_rate=pace.google_rate,
        voice_gender=gender,
        voice_name=voice_name,
    )

    last_err: Exception | None = None
    last_raw = ""
    # Script-parse mode tends to be lighter than topic-gen (the model isn't
    # writing prose), but we still need enough headroom for 12-15 scene
    # detailed responses where each scene echoes a paragraph of the input.
    max_tokens = 20480 if duration_mode == "detailed" else 8192

    for attempt in range(1, MAX_ATTEMPTS + 1):
        # Lower temperature — we want the model to PARSE faithfully, not
        # invent. Slight bump on retry to escape repeating failure modes.
        temperature = 0.35 if attempt == 1 else 0.5
        try:
            resp = await asyncio.to_thread(
                client.models.generate_content,
                model=model,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                ),
            )
        except Exception as exc:
            last_err = exc
            transient = is_transient_quota_error(exc)
            logger.warning(
                "plan_story_from_script attempt %d Gemini error %s%s: %s",
                attempt, type(exc).__name__,
                " (transient — will rotate key + backoff)" if transient else "",
                exc,
            )
            if attempt < MAX_ATTEMPTS and transient:
                client = get_client()
                await asyncio.sleep(2 * attempt)
            continue

        last_raw = resp.text or ""
        try:
            data = parse_json_loose(last_raw)
        except json.JSONDecodeError as exc:
            last_err = exc
            logger.warning(
                "plan_story_from_script attempt %d JSON parse failed: %s",
                attempt, exc,
            )
            user_prompt = _build_repair_prompt(script_norm[:200], last_raw, exc)
            continue

        if not isinstance(data, dict):
            last_err = ValueError(f"expected JSON object, got {type(data).__name__}")
            continue

        # Same defensive backfill as topic mode.
        data.setdefault("title", script_norm.split("\n")[0][:120].strip())
        data["language"] = target_lang
        data["aspect_ratio"] = aspect_ratio
        data["media_source"] = media_source
        data["duration_mode"] = duration_mode
        data["theme"] = theme
        data["voice_pace_name"] = pace.name
        data["voice_edge_rate"] = pace.edge_rate
        data["voice_google_rate"] = pace.google_rate
        data["voice_gender"] = gender
        data["voice_name"] = voice_name

        if media_source != "ai_image":
            data["characters"] = []
            data["character_brief"] = None
            data["setting_brief"] = None

        scenes = data.get("scenes") or []
        for i, s in enumerate(scenes, start=1):
            if isinstance(s, dict):
                s["scene_index"] = i
                s["kind"] = s.get("kind") or "scene"

        try:
            plan = TellaScenePlan.model_validate(data)
        except ValidationError as exc:
            last_err = exc
            logger.warning(
                "plan_story_from_script attempt %d validation failed: %s",
                attempt, exc,
            )
            user_prompt = _build_repair_prompt(script_norm[:200], last_raw, exc)
            continue

        apply_lock(plan, style_suffix=theme_spec.image_style_suffix)
        logger.info(
            "plan_story_from_script succeeded (%d scenes, attempt %d)",
            len(plan.scenes), attempt,
        )
        return plan

    assert last_err is not None
    logger.error(
        "plan_story_from_script gave up after %d attempts: %s",
        MAX_ATTEMPTS, last_err,
    )
    raise last_err


def plan(**kwargs) -> TellaScenePlan:
    """Sync wrapper for CLI / smoke tests."""
    return asyncio.run(plan_story(**kwargs))


__all__ = [
    "MAX_ATTEMPTS",
    "plan",
    "plan_story",
    "plan_story_from_script",
]
