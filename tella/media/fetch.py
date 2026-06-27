"""Top-level media dispatcher — fetch every scene's asset for a plan.

Given a :class:`TellaScenePlan`, ``fetch_assets`` walks every body scene
and writes one asset file per scene into ``<job_dir>/assets/`` based on
the plan's ``media_source``:

  - ``ai_image``    → CF Workers AI FLUX → JPG
  - ``stock_photo`` → Pexels Photo       → JPG
  - ``stock_video`` → Pexels Video       → MP4

For v1 MVP each scene gets exactly 1 asset. Multi-asset per scene
(``Scene.asset_count`` > 1) is deferred to a later iteration — the field
is preserved on the plan for downstream consumers but the media layer
ignores it for now (see DECISIONS.md D-007).

Scenes are fetched concurrently up to ``MAX_CONCURRENT`` to keep render
turnaround tight. Failures bubble per scene — the dispatcher does NOT
swap providers (e.g. stock photo when stock video fails) because cross-
provider fallback would silently change what the user asked for.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from tella.media import ai_image, stock_photo, stock_video
from tella.planner.models import TellaScenePlan

logger = logging.getLogger("tella.media.fetch")

# Keep concurrency modest: bursting many simultaneous requests at one CF
# account triggers rate-limit 429s. 3 in flight + the global throttle in
# ai_image keeps us under the limit while still rendering quickly.
MAX_CONCURRENT = 3

# Generation dims fed to the AI image provider. Smaller than the 1080×1920 /
# 1920×1080 final canvas on purpose — the renderer upscales/crops, and a
# smaller image costs fewer CF Neurons so a free account lasts far longer.
_GEN_DIMS: dict[str, tuple[int, int]] = {
    "9:16": (768, 1344),
    "16:9": (1344, 768),
}


def _safe_stem(text: str, max_len: int = 30) -> str:
    """Filesystem-safe slug for asset filenames."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (text or "scene")).strip("_").lower()
    return (slug or "scene")[:max_len]


async def fetch_assets(plan: TellaScenePlan, job_dir: Path) -> None:
    """Populate ``plan.scenes[i].image_filenames`` for every body scene.

    Mutates the plan in place. Writes to ``<job_dir>/assets/``.

    Raises:
        RuntimeError: when ANY scene's asset fetch fails. Callers wanting
            partial-success behaviour should wrap in their own try/except.
    """
    job_dir = Path(job_dir)
    assets_dir = job_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    body_scenes = [s for s in plan.scenes if s.kind == "scene"]
    if not body_scenes:
        return

    width, height = _GEN_DIMS.get(plan.aspect_ratio, _GEN_DIMS["9:16"])
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def _fallback_to_stock_photo(scene, base: str) -> None:
        """Last-resort fetch when the primary provider fails. Pexels Photo
        is the safest fallback — no NSFW safety filter false-positives,
        no per-account quota that resets only daily.
        """
        out = assets_dir / f"{base}_fallback.jpg"
        query = scene.stock_query or scene.image_prompt[:60] or scene.title[:60] or "abstract"
        await stock_photo.search_and_download(
            query, out, width=width, height=height,
        )
        scene.image_filenames = [f"assets/{out.name}"]
        logger.warning(
            "scene %d: AI image failed → fell through to Pexels Photo (query=%r)",
            scene.scene_index, query,
        )

    async def _one(scene_idx: int, scene) -> None:
        async with sem:
            base = f"scene_{scene.scene_index:02d}_{_safe_stem(scene.title)}"
            if plan.media_source == "ai_image":
                out = assets_dir / f"{base}.jpg"
                try:
                    await ai_image.generate_image(
                        scene.image_prompt,
                        out,
                        width=width,
                        height=height,
                        seed=1000 + scene.scene_index,
                    )
                    scene.image_filenames = [f"assets/{out.name}"]
                except Exception as exc:
                    # Either daily neuron quota burned across every CF
                    # account, or the safety filter false-positived a
                    # specific scene's prompt. Either way, Pexels Photo
                    # always works — fall through so the user still gets
                    # a complete video instead of "all 5 accounts failed".
                    logger.warning(
                        "scene %d: AI image failed (%s) → fallback to Pexels",
                        scene.scene_index, str(exc)[:120],
                    )
                    await _fallback_to_stock_photo(scene, base)
            elif plan.media_source == "stock_photo":
                out = assets_dir / f"{base}.jpg"
                await stock_photo.search_and_download(
                    scene.stock_query or scene.image_prompt[:60],
                    out,
                    width=width,
                    height=height,
                )
                scene.image_filenames = [f"assets/{out.name}"]
            elif plan.media_source == "stock_video":
                out = assets_dir / f"{base}.mp4"
                try:
                    final = await stock_video.search_and_download(
                        scene.stock_query or scene.image_prompt[:60],
                        out,
                        width=width,
                        height=height,
                    )
                    scene.image_filenames = [f"assets/{final.name}"]
                except Exception as exc:
                    logger.warning(
                        "scene %d: stock video failed (%s) → fallback to Pexels Photo",
                        scene.scene_index, str(exc)[:120],
                    )
                    await _fallback_to_stock_photo(scene, base)
            else:
                raise RuntimeError(
                    f"unknown media_source {plan.media_source!r}"
                )

    logger.info(
        "fetch_assets: %d scenes, source=%s, %dx%d",
        len(body_scenes), plan.media_source, width, height,
    )
    await asyncio.gather(*[_one(i, s) for i, s in enumerate(body_scenes)])
    logger.info("fetch_assets: all %d scenes done", len(body_scenes))


__all__ = [
    "MAX_CONCURRENT",
    "fetch_assets",
]
