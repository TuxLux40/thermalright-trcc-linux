"""Video frame cache — lazy per-frame surface adjustment.

Two-layer cache:
  L2: Frames + theme mask (pre-composited at load time, immutable)
  L3: Brightness+rotation-adjusted native surfaces per frame.
      Fills lazily during the first playback loop — each frame is
      adjusted once on first access and reused every subsequent loop.

Per-tick pipeline (in DisplayService.video_tick):
  1. get_surface(index)  → L3 brightness+rotation surface
  2. composite text_overlay  (same surface for ALL frames — rendered once
     per metrics refresh interval, not once per frame)
  3. encode_for_device   → bytes  (one encode per tick, not per rebuild)
  4. send to USB

Text overlay (from OverlayService.render_text_only) is stored once via
update_text_overlay() — called at most once per refresh interval.
No background threads, no 147-frame encode loop on metrics change.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class VideoFrameCache:
    """Video frame cache with lazy per-frame L3 surface adjustment.

    L2 (video + mask) is built once at load time.
    L3 (brightness+rotation-adjusted surfaces) fills during the first
    playback loop. After one full loop, every get_surface() call is a
    pure list lookup — no compositing, no per-frame work.

    Text overlay is managed separately: stored once per metrics refresh
    via update_text_overlay(), composited by DisplayService at tick time.
    """

    def __init__(self) -> None:
        # L2: video frames + mask composite (immutable after build)
        self._masked_frames: list[Any] = []

        # Text overlay (rendered once per refresh interval by OverlayService)
        self._text_overlay: Any | None = None
        self._text_key: tuple | None = None

        # Brightness / rotation state
        self._brightness: int = 100
        self._rotation: int = 0

        # Encoding params (from DeviceInfo — set at build time)
        self._protocol: str = 'scsi'
        self._resolution: tuple[int, int] | None = None
        self._fbl: int | None = None
        self._use_jpeg: bool = False
        self._encode_angle: int = 0

        # L3: per-frame brightness+rotation-adjusted native surfaces.
        # None = not yet adjusted for this frame index.
        self._l3_surfaces: list[Any | None] = []
        self._l3_brightness: int = 100
        self._l3_rotation: int = 0

        self._active: bool = False

    # -- Properties --------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._active and bool(self._masked_frames)

    @property
    def text_overlay(self) -> Any | None:
        """Current text overlay surface, or None if overlay disabled."""
        return self._text_overlay

    @property
    def has_text(self) -> bool:
        """True if a text overlay surface is currently stored."""
        return self._text_overlay is not None

    @property
    def encoding_params(self) -> tuple[str, tuple[int, int] | None, int | None, bool, int]:
        """(protocol, resolution, fbl, use_jpeg, encode_angle) for encode_for_device."""
        return self._protocol, self._resolution, self._fbl, self._use_jpeg, self._encode_angle

    # -- Full build (video load) -------------------------------------------

    def build(
        self,
        frames: list[Any],
        mask: Any | None,
        mask_position: tuple[int, int],
        brightness: int,
        rotation: int,
        protocol: str,
        resolution: tuple[int, int],
        fbl: int | None,
        use_jpeg: bool,
        encode_angle: int = 0,
    ) -> None:
        """Build L2 cache. Safe to call from a background thread."""
        if not frames:
            return

        from .image import ImageService
        r = ImageService.renderer()

        # Convert frames to native surfaces if needed
        from ..core.ports import RawFrame
        first = frames[0]
        if isinstance(first, RawFrame):
            frames = [r.from_raw_rgb24(f) for f in frames]

        self._brightness = brightness
        self._rotation = rotation
        self._protocol = protocol
        self._resolution = resolution
        self._fbl = fbl
        self._use_jpeg = use_jpeg
        self._encode_angle = encode_angle

        self._build_layer2(frames, mask, mask_position)
        self._reset_l3()
        self._active = True
        log.info("VideoFrameCache: built %d frames", len(self._masked_frames))

    # -- Text overlay update (once per refresh interval) ------------------

    def update_text_overlay(self, surface: Any | None, key: tuple | None) -> bool:
        """Store a new text overlay surface. Returns True if text changed.

        Called at most once per metrics refresh interval — O(1), no frame loop.
        The surface is the same for every frame; DisplayService composites it
        onto the current frame at tick time.
        """
        if key == self._text_key:
            return False
        self._text_overlay = surface
        self._text_key = key
        return True

    def clear_text_overlay(self) -> None:
        """Clear text overlay (overlay disabled)."""
        self._text_overlay = None
        self._text_key = None

    # -- Partial rebuilds (brightness / rotation change) ------------------

    def rebuild_from_brightness(self, brightness: int) -> None:
        """Update brightness. L3 slots refill naturally on next access."""
        if not self._masked_frames:
            return
        self._brightness = brightness
        self._reset_l3()

    def rebuild_from_rotation(self, rotation: int) -> None:
        """Update rotation. L3 slots refill naturally on next access."""
        if not self._masked_frames:
            return
        self._rotation = rotation
        self._reset_l3()

    # -- Per-tick access ---------------------------------------------------

    def get_surface(self, index: int) -> Any | None:
        """Return brightness+rotation-adjusted surface for frame index.

        Text overlay is NOT composited here — DisplayService does it per tick
        so the same text surface is reused across all 147 frames without
        any frame loop.

        Returns None if index is out of range or cache is not built.
        """
        if not (0 <= index < len(self._masked_frames)):
            return None
        self._ensure_surface(index)
        return self._l3_surfaces[index]

    # -- Private -----------------------------------------------------------

    def _ensure_surface(self, index: int) -> None:
        """Apply brightness to L2 frame → L3 surface if not cached.

        Rotation is NOT applied here — `encode_for_device` is the sole
        rotator on the encode boundary so every element (bg + mask + text)
        ends up with the same rotation count.  Layer-3 stays in source
        coord space; text overlay composited at tick time aligns naturally.
        """
        if self._brightness != self._l3_brightness:
            self._reset_l3()

        if self._l3_surfaces[index] is not None:
            return  # L3 hit — pure list lookup

        from .image import ImageService

        if self._brightness < 100:
            r = ImageService.renderer()
            surface = r.copy_surface(self._masked_frames[index])
            surface = ImageService.apply_brightness(surface, self._brightness)
        else:
            surface = self._masked_frames[index]

        self._l3_surfaces[index] = surface

    def _reset_l3(self) -> None:
        """Clear all L3 slots. They refill lazily during the next loop."""
        n = len(self._masked_frames)
        self._l3_surfaces = [None] * n
        self._l3_brightness = self._brightness

    def _build_layer2(
        self,
        frames: list[Any],
        mask: Any | None,
        mask_position: tuple[int, int],
    ) -> None:
        """Composite mask onto each video frame → _masked_frames.

        If no mask, L2 references frames directly (zero copy).
        """
        if mask is None:
            self._masked_frames = list(frames)
            return

        from .image import ImageService
        r = ImageService.renderer()
        mask_rgba = r.convert_to_rgba(mask)
        self._masked_frames = []
        for frame in frames:
            composited = r.copy_surface(frame)
            composited = r.composite(composited, mask_rgba, mask_position)
            self._masked_frames.append(composited)
