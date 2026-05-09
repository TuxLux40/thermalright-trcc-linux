"""RenderPipeline — pure rendering pipeline for DisplayService.

Composed into DisplayService and reads state via a one-way back-reference.
The pipeline owns nothing the service shouldn't see (the split-overlay
asset cache lives here because it's a pure-render concern); state that
mutates with theme/rotation/brightness stays on DisplayService.

The rendering chain is:

    current_image → overlay.render → apply_adjustments → apply_for_preview

Each step is its own method so callers can opt into part of the chain
(``apply_adjustments`` without preview rotation for encode-bound paths;
``apply_for_preview`` to wrap the output with user rotation for the GUI).
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from ..core.models import SPLIT_OVERLAY_MAP
from ..core.paths import RESOURCES_DIR
from .image import ImageService

if TYPE_CHECKING:
    from .display import DisplayService

log = logging.getLogger(__name__)


class RenderPipeline:
    """Pure render pipeline — composite, brightness, split overlay, preview rotation."""

    def __init__(self, svc: DisplayService) -> None:
        self._svc = svc
        # Pre-loaded split-overlay surfaces, keyed by (style, rotation).
        self._split_cache: dict[tuple[int, int], Any] = {}

    # ── Top-level entry points ──────────────────────────────────────────────

    def render_and_process(self) -> Any | None:
        """Render overlay on current image, apply brightness + preview rotation."""
        s = self._svc
        if not s.current_image:
            s.log.debug("_render_and_process: no current_image")
            return None
        image = s.current_image
        s.log.debug(
            "_render_and_process: current_image type=%s overlay_enabled=%s",
            type(image).__name__, s.overlay.enabled,
        )
        if s.overlay.enabled:
            image = s.overlay.render(image)
            s.log.debug("_render_and_process: after overlay type=%s",
                        type(image).__name__)
        return self.apply_for_preview(self.apply_adjustments(image))

    def render_overlay_force(self) -> Any | None:
        """Force-render overlay (for live editing).  Returns rotated preview image."""
        s = self._svc
        bg = s._clean_background or s.current_image
        if not bg:
            s.log.debug("render_overlay: no background, creating black bg")
            s._create_black_background()
            bg = s.current_image
        image = s.overlay.render(bg, force=True)
        return self.apply_for_preview(self.apply_adjustments(image))

    # ── Pipeline stages ─────────────────────────────────────────────────────

    def apply_adjustments(self, image: Any) -> Any:
        """Apply brightness + split overlay.  No pixel rotation here.

        Rotation is the encode boundary's responsibility (``encode_for_device``
        with ``encode_angle``) so every element (bg + mask + text) ends up with
        the same rotation count.  Preview consumers wrap this with
        ``apply_for_preview`` to add a single user-rotation for display.
        """
        s = self._svc
        s.log.debug("_apply_adjustments: brightness=%d split_mode=%d",
                    s.brightness, s.split_mode)
        if s.brightness >= 100 and not s.split_mode:
            return image
        if s.brightness < 100:
            image = ImageService.apply_brightness(image, s.brightness)
        return self.apply_split_overlay(image)

    def apply_for_preview(self, image: Any) -> Any:
        """Wrap ``apply_adjustments`` output with user rotation for GUI preview.

        Encode-bound paths bypass this — they go through
        ``encode_for_device(..., encode_angle=svc._encode_angle())`` which
        is the sole rotator for device bytes.
        """
        rot = self._svc._image_rotation
        if rot:
            image = ImageService.apply_rotation(image, rot)
        return image

    def apply_split_overlay(self, image: Any) -> Any:
        """Composite Dynamic Island overlay for widescreen split mode."""
        s = self._svc
        if not s.split_mode or not s.is_widescreen_split:
            return image

        key = (s.split_mode, s.rotation)
        if not (asset_name := SPLIT_OVERLAY_MAP.get(key)):
            return image

        overlay = self._split_cache.get(key)
        if overlay is None:
            overlay = self._load_split_asset(asset_name)
            self._split_cache[key] = overlay

        if overlay is None:
            return image

        try:
            r = ImageService.renderer()
            image = r.convert_to_rgba(image)
            img_w, img_h = r.surface_size(image)
            ovl_w, ovl_h = r.surface_size(overlay)
            if (ovl_w, ovl_h) != (img_w, img_h):
                overlay = r.resize(overlay, img_w, img_h)
            image = r.composite(image, overlay, (0, 0))
            return r.convert_to_rgb(image)
        except (OSError, ValueError, RuntimeError) as e:
            s.log.error("Split overlay composite failed: %s", e)
            return image

    @staticmethod
    def _load_split_asset(asset_name: str) -> Any | None:
        """Load a split overlay PNG from assets/gui/ as native surface."""
        try:
            path = os.path.join(RESOURCES_DIR, asset_name)
            if os.path.exists(path):
                r = ImageService.renderer()
                img = r.open_image(path)
                return r.convert_to_rgba(img)
            log.warning("Split overlay not found: %s", path)
        except (OSError, ValueError, RuntimeError) as e:
            log.error("Failed to load split overlay %s: %s", asset_name, e)
        return None
