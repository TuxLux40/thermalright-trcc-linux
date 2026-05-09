"""Display pipeline orchestrator — coordinates theme, overlay, media -> LCD frame.

Pure Python, no Qt dependencies.
Controllers (PySide6, Typer CLI, FastAPI) are thin wrappers that call this
service and fire callbacks.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.ports import Platform

from ..core._logging import tagged_logger
from ..core.models import SPLIT_MODE_RESOLUTIONS, SPLIT_OVERLAY_MAP
from ..core.paths import (
    RESOURCES_DIR,
    has_themes,
    masks_dir_name,
    theme_dir_name,
    web_dir_name,
)
from .device import DeviceService
from .image import ImageService
from .media import MediaService
from .overlay import OverlayService
from .theme_loader import ThemeLoader
from .theme_persistence import ThemePersistence

log = logging.getLogger(__name__)


class DisplayService:
    """Display pipeline: theme -> overlay -> brightness/rotation -> LCD frame.

    Orchestrates sub-services (DeviceService, OverlayService, MediaService).
    Sub-services are injected, not owned. Theme loading and persistence
    delegated to ThemeLoader and ThemePersistence (SRP).
    """

    def __init__(self,
                 devices: DeviceService,
                 overlay: OverlayService,
                 media: MediaService,
                 theme_svc: Any = None,
                 cpu_percent_fn: Callable[[], float] | None = None,
                 path_resolver: Platform | None = None,
                 device_label: str = '') -> None:
        # Per-device child logger — tags every record with device identity
        self.log: logging.Logger = tagged_logger(__name__, device_label)

        # Sub-services (injected)
        self.devices = devices
        self.overlay = overlay
        self.media = media
        self._cpu_percent_fn = cpu_percent_fn
        self._path_resolver = path_resolver

        # Per-device resolution (owned by this instance, not the Settings singleton)
        self._width = 0
        self._height = 0

        # Theme loader (injected with same sub-services)
        self._loader = ThemeLoader(overlay, media, theme_svc=theme_svc)
        self._persistence = ThemePersistence(theme_svc=theme_svc)

        # Working directory (Windows GifDirectory pattern)
        self.working_dir = Path(tempfile.mkdtemp(prefix='trcc_work_'))

        # State
        self.current_image: Any | None = None  # Native surface (QImage)
        self._clean_background: Any | None = None  # Original bg before overlay
        self.current_theme_path: Path | None = None
        self.auto_send = True
        self.brightness = 100     # percent (0-100), config restores actual value
        self.split_mode = 0       # myLddVal: 0=off, 1-3=Dynamic Island style
        self._split_overlay_cache: dict[tuple[int, int], Any] = {}  # (style,rot)->surface

        # Data directory (set by initialize(), used as fallback for save/import)
        self._data_dir: Path | None = None

        # Pre-baked video frame cache (None when inactive)
        self._cache: Any | None = None  # VideoFrameCache

        # Callback: fired when background data extraction finishes
        self.on_data_ready: Any | None = None

        # Display geometry primitives (was the Orientation class).
        # All directory + resolution derivations are @property below.
        self._native: tuple[int, int] = (0, 0)
        self._rotation: int = 0
        self._data_root: Path | None = None
        self._user_root: Path | None = None
        self._has_portrait_themes: bool = False

        # Mask source tracking (for rotation reload)
        self._mask_source_dir: Path | None = None

    # -- Properties --------------------------------------------------------

    @property
    def lcd_width(self) -> int:
        return self._width

    @property
    def lcd_height(self) -> int:
        return self._height

    @property
    def lcd_size(self) -> tuple[int, int]:
        return (self._width, self._height)

    @property
    def native_resolution(self) -> tuple[int, int]:
        """Native (handshake) resolution. Doesn't change after connect."""
        return self._native

    @property
    def rotation(self) -> int:
        return self._rotation

    @rotation.setter
    def rotation(self, value: int) -> None:
        self._rotation = value

    def is_rotated(self) -> bool:
        """True when rotation is 90/270 on a non-square device."""
        w, h = self._native
        return w != h and self._rotation in (90, 270)

    def _rotated_resolution(self) -> tuple[int, int]:
        """Native with w,h swapped if rotated."""
        w, h = self._native
        return (h, w) if self.is_rotated() else (w, h)

    @property
    def output_resolution(self) -> tuple[int, int]:
        """Physical device output — always swaps for non-square at 90/270."""
        return self._rotated_resolution()

    @property
    def canvas_resolution(self) -> tuple[int, int]:
        """Internal rendering resolution — only swaps when portrait themes exist."""
        if self._has_portrait_themes and self.is_rotated():
            w, h = self._native
            return (h, w)
        return self._native

    @property
    def effective_resolution(self) -> tuple[int, int]:
        """Canvas rendering resolution — only swaps when portrait dirs exist."""
        return self.canvas_resolution

    @property
    def canvas_size(self) -> tuple[int, int]:
        """Alias for effective_resolution."""
        return self.canvas_resolution

    @property
    def has_portrait_themes(self) -> bool:
        return self._has_portrait_themes

    def image_rotation_for(self, overlay_w: int, overlay_h: int) -> int:
        """Pixel rotation taking overlay aspect into account.

        0 when portrait theme dirs handle orientation (content already
        portrait), or when the overlay (mask + text composite) is itself
        in portrait dimensions — the canvas already represents the rotated
        frame so no further pixel rotation is needed at this layer.
        """
        if not self.is_rotated():
            return self._rotation
        if self._has_portrait_themes or overlay_h > overlay_w:
            return 0
        return self._rotation

    @property
    def _image_rotation(self) -> int:
        """Pixel rotation angle. 0 when content is already portrait."""
        return self.image_rotation_for(self.overlay.width, self.overlay.height)

    def _encode_angle(self) -> int:
        """Device encode rotation angle (C# RotateImg in ImageToJpg)."""
        from ..core.models import get_encode_rotation, get_profile
        dev = self.devices.selected
        if not dev or dev.fbl_code is None:
            return 0
        profile = get_profile(dev.fbl_code, dev.pm_byte)
        return get_encode_rotation(
            profile, dev.sub_byte, self.rotation, pm_byte=dev.pm_byte,
        )

    @property
    def mask_source_dir(self) -> Path | None:
        return self._mask_source_dir

    @mask_source_dir.setter
    def mask_source_dir(self, value: Path | None) -> None:
        self._mask_source_dir = value

    @property
    def clean_background(self) -> Any | None:
        return self._clean_background

    def invalidate_video_cache(self) -> None:
        self._cache = None

    def convert_media_frames(self) -> None:
        self._convert_media_frames()

    def render_and_process(self) -> Any | None:
        return self._render_and_process()

    # -- Initialization ----------------------------------------------------

    def initialize(self, data_dir: Path) -> None:
        """Initialize service with data directory."""
        self.log.debug("DisplayService: init data_dir=%s", data_dir)
        self._data_dir = data_dir

        cw, ch = self.canvas_size
        if cw and ch:
            self.media.set_target_size(cw, ch)
            self.overlay.set_resolution(cw, ch)
            self._setup_dirs(self._width, self._height)

    def _setup_dirs(self, width: int, height: int) -> None:
        """Set content roots and probe portrait theme availability.

        Dirs are derived from roots + resolution — no stored dir lists.
        Only non-derivable fact probed: do portrait themes exist on disk?
        """
        pr = self._path_resolver

        if pr:
            self._data_root = Path(pr.data_dir())
            self._user_root = Path(pr.user_content_dir()) / 'data'
        else:
            from ..core.paths import DATA_DIR
            self._data_root = Path(DATA_DIR)
            self._user_root = None

        # Probe portrait themes — the only non-derivable fact
        sw, sh = height, width
        self._has_portrait_themes = (
            width != height
            and has_themes(str(self._data_root / theme_dir_name(sw, sh)))
        )
        self.log.info("Geometry: data_root=%s user_root=%s has_portrait_themes=%s",
                 self._data_root, self._user_root, self._has_portrait_themes)

    def cleanup(self) -> None:
        """Clean up working directory on exit."""
        if self.working_dir and self.working_dir.exists():
            shutil.rmtree(self.working_dir, ignore_errors=True)

    # -- Resolution --------------------------------------------------------

    def set_resolution(self, width: int, height: int) -> None:
        """Set LCD resolution and update sub-services."""
        if width == self._width and height == self._height:
            self.log.debug("set_resolution: no change (%dx%d)", width, height)
            return
        self.log.info("Resolution changed: %dx%d -> %dx%d",
                 self._width, self._height, width, height)
        self._width = width
        self._height = height
        # _native re-pinned; rotation preserved across the resize.
        # _has_portrait_themes will be re-probed below by _setup_dirs().
        self._native = (width, height)
        self._has_portrait_themes = False

        if width and height:
            self._setup_dirs(width, height)

        cw, ch = self.canvas_size
        self.media.set_target_size(cw, ch)
        self.overlay.set_resolution(cw, ch)
        self.log.info("set_resolution: canvas=%s output=%s image_rotation=%d",
                 self.canvas_size, self.output_resolution, self._image_rotation)

    def refresh_dirs(self) -> None:
        """Re-probe filesystem for current resolution.

        Called after DATA_READY — new content may have been extracted.
        """
        if self._width and self._height:
            self._setup_dirs(self._width, self._height)

    # -- Display adjustments -----------------------------------------------

    def set_rotation(self, degrees: int) -> Any | None:
        """Set display rotation. Returns rendered image or None.

        Two behaviors:
        - has_portrait_themes=True: canvas re-inits at portrait dims, dirs swap
        - has_portrait_themes=False: canvas stays, composited output gets pixel-rotated
        """
        old_canvas = self.canvas_size
        self.rotation = degrees % 360
        new_canvas = self.canvas_size

        self.log.info("set_rotation: %d° canvas %s→%s portrait_themes=%s image_rotation=%d",
                 degrees, old_canvas, new_canvas,
                 self._has_portrait_themes, self._image_rotation)

        if old_canvas != new_canvas:
            cw, ch = new_canvas
            self.overlay.set_resolution(cw, ch)
            self.media.set_target_size(cw, ch)
            self._cache = None
        elif (cache := self._cache) and cache.active:
            cache.rebuild_from_rotation(self._image_rotation)

        return self._render_and_process()

    def set_brightness(self, percent: int) -> Any | None:
        """Set display brightness. Returns rendered image or None."""
        self.brightness = max(0, min(100, percent))
        if (cache := self._cache) and cache.active:
            cache.rebuild_from_brightness(self.brightness)
        return self._render_and_process()

    def set_split_mode(self, mode: int) -> Any | None:
        """Set split mode (C# myLddVal: 0=off, 1-3=Dynamic Island style).

        Only affects 1600x720 widescreen devices. Returns rendered image.
        """
        self.split_mode = mode if mode in (0, 1, 2, 3) else 0
        return self._render_and_process()

    @property
    def is_widescreen_split(self) -> bool:
        """True if current resolution supports split mode."""
        return self.lcd_size in SPLIT_MODE_RESOLUTIONS

    # -- Frame conversion --------------------------------------------------

    def _convert_media_frames(self) -> None:
        """Convert decoded frames to native renderer surfaces.

        RawFrame (from VideoDecoder/ThemeZtDecoder) → native surface via renderer.
        Converts in-place once at load time.
        """
        from ..core.ports import RawFrame
        frames = self.media._frames
        if not frames:
            return
        r = ImageService.renderer()
        first = frames[0]
        if isinstance(first, RawFrame):
            self.media._frames = [r.from_raw_rgb24(f) for f in frames]
        else:
            # Already native surfaces (QImage) — use as-is
            self.media._frames = list(frames)

    # -- Theme loading (delegates to ThemeLoader) --------------------------

    def load_local_theme(self, theme) -> dict:
        """Load a local theme with DC config, mask, and overlay."""
        self._cache = None  # Invalidate previous video cache
        result = self._loader.load_local_theme(
            theme, self.canvas_size, self.working_dir)

        # Convert decoded frames to native renderer surfaces (if animated)
        if result.get('is_animated'):
            self._convert_media_frames()

        # Wire up state from loader result
        self._mask_source_dir = result.get('mask_source_dir')
        self.current_theme_path = result.get('theme_path')
        self.log.debug("load_local_theme: _mask_source_dir=%s", self._mask_source_dir)

        # Set current_image from result or from video first frame
        if result.get('image'):
            self.current_image = result['image']
            self._clean_background = result['image']
        elif result.get('is_animated'):
            first_frame = self.media.get_frame(0)
            if first_frame:
                self.current_image = first_frame
                self._clean_background = first_frame

        # Build cache in background — avoids 650ms GUI freeze on theme select
        if result.get('is_animated') and self.media.has_frames and self.devices.selected:
            self._start_video_cache_async()

        # Render with adjustments if we have a static image
        if result.get('image') and not result.get('is_animated'):
            result['image'] = self._render_and_process()

        return result

    def load_cloud_theme(self, theme) -> dict:
        """Load a cloud video theme as background."""
        self._cache = None  # Invalidate previous video cache
        # Decode video frames at overlay's current dimensions — mask/DC stay
        self.media.set_target_size(self.overlay.width, self.overlay.height)
        result = self._loader.load_cloud_theme(theme, self.working_dir)
        self.log.debug("load_cloud_theme: loader result keys=%s", list(result.keys()))

        # Wire up state — cloud themes are video-only, so preserve
        # existing mask source dir (user may have applied a mask before
        # selecting the cloud video background)
        if result.get('mask_source_dir') is not None:
            self._mask_source_dir = result['mask_source_dir']
        self.current_theme_path = result.get('theme_path')

        # Convert decoded frames to native renderer surfaces
        self._convert_media_frames()
        self.log.debug("load_cloud_theme: frames converted, count=%d",
                  len(self.media._frames) if self.media._frames else 0)

        first_frame = self.media.get_frame(0)
        self.log.debug("load_cloud_theme: first_frame=%s",
                  type(first_frame).__name__ if first_frame else None)
        if first_frame:
            self.current_image = first_frame
            self._clean_background = first_frame
        result['image'] = self.current_image

        # Build cache in background — avoids GUI freeze on cloud theme load
        if self.media.has_frames:
            self.log.debug("load_cloud_theme: starting async video cache build")
            self._start_video_cache_async()
            self.log.debug("load_cloud_theme: async cache build started")
        return result

    def apply_standalone_mask(
        self, mask_path: Path, dc_config_cls: Any, *,
        is_rotated: bool,
    ) -> dict:
        """Apply an arbitrary mask file/dir to the active overlay.

        Resolves the mask file (single PNG or directory's ``01.png``),
        computes its position from the sibling ``config1.dc`` if any,
        toggles overlay state, and returns ``{image, mask_file}``.
        For zt portrait masks the overlay resolution is bumped to the
        rotated output size before placement.
        """
        from .overlay import OverlayService

        mask_dir = mask_path if mask_path.is_dir() else mask_path.parent
        is_zt = mask_dir.parent.name.startswith('zt')
        if is_zt and is_rotated:
            w, h = self.output_resolution
            self.overlay.set_resolution(w, h)
            self.log.info("apply_standalone_mask: portrait zt mask → overlay %dx%d", w, h)
        else:
            w, h = self.canvas_size

        if mask_path.is_dir():
            mask_file = mask_path / "01.png"
            if not mask_file.exists():
                mask_file = next(mask_path.glob("*.png"), None)
            if not mask_file:
                return {"success": False, "error": f"No PNG files in {mask_path}"}
        else:
            mask_file = mask_path

        r = ImageService.renderer()
        mask_img = r.convert_to_rgba(r.open_image(mask_file))
        mask_w, mask_h = r.surface_size(mask_img)
        dc_path = mask_dir / 'config1.dc'
        position = OverlayService.calculate_mask_position(
            dc_config_cls, dc_path, (mask_w, mask_h), (w, h))

        self.overlay.set_theme_mask(None)
        self.overlay.set_mask(mask_img, position)
        self.overlay.enabled = True
        self._mask_source_dir = mask_dir
        self.log.debug("apply_standalone_mask: _mask_source_dir=%s", self._mask_source_dir)
        bg = self._clean_background or self.current_image \
            or ImageService.solid_color(0, 0, 0, w, h)
        self.current_image = bg
        self.invalidate_video_cache()
        return {"success": True, "image": self.render_overlay(),
                "mask_file": mask_file}

    def apply_mask(self, mask_dir: Path) -> Any | None:
        """Apply a mask overlay on top of current content."""
        # Restore clean background so old mask isn't baked in
        if self._clean_background is not None:
            self.current_image = self._clean_background
        elif not self.current_image:
            self.current_image = ImageService.solid_color(0, 0, 0, *self.canvas_size)

        self._mask_source_dir = self._loader.apply_mask(
            mask_dir, self.working_dir, self.canvas_size)
        self.log.debug("apply_mask: _mask_source_dir=%s", self._mask_source_dir)

        # Rebuild cache async — new mask must be composited into L2
        self._cache = None
        if self.media.has_frames:
            self._start_video_cache_async()

        return self.render_overlay()

    # -- Image loading (kept on DisplayService -- tied to state) -----------

    def load_image_file(self, path: Path) -> Any | None:
        """Load a static image file. Returns rendered image or None."""
        self._load_static_image(path)
        return self._render_and_process()

    def set_clean_background(self, image: Any) -> None:
        """Set both current_image and clean_background to a native surface.

        Used when loading a custom background image (C# imagePicture + bitmapBGK).
        """
        self._clean_background = image
        self.current_image = image

    def _load_static_image(self, path: Path) -> None:
        """Load and resize a static image to canvas dimensions."""
        try:
            self.current_image = ImageService.open_and_resize(path, *self.canvas_size)
            self._clean_background = self.current_image
        except (OSError, ValueError, RuntimeError) as e:
            self.log.error("Failed to load image: %s", e)

    def _create_black_background(self) -> None:
        """Create black background for mask-only themes."""
        self.current_image = ImageService.solid_color(0, 0, 0, *self.canvas_size)

    # -- Rendering ---------------------------------------------------------

    def _render_and_process(self) -> Any | None:
        """Render overlay on current image, apply brightness + preview rotation."""
        if not self.current_image:
            self.log.debug("_render_and_process: no current_image")
            return None
        image = self.current_image
        self.log.debug("_render_and_process: current_image type=%s overlay_enabled=%s",
                  type(image).__name__, self.overlay.enabled)
        if self.overlay.enabled:
            image = self.overlay.render(image)
            self.log.debug("_render_and_process: after overlay type=%s", type(image).__name__)
        return self._apply_for_preview(self._apply_adjustments(image))

    def render_overlay(self) -> Any | None:
        """Force-render overlay (for live editing). Returns rotated preview image."""
        # Use clean background (no old overlay baked in)
        bg = self._clean_background or self.current_image
        if not bg:
            self.log.debug("render_overlay: no background, creating black bg")
            self._create_black_background()
            bg = self.current_image
        image = self.overlay.render(bg, force=True)
        return self._apply_for_preview(self._apply_adjustments(image))

    def _apply_adjustments(self, image: Any) -> Any:
        """Apply brightness + split overlay.  No pixel rotation here.

        Rotation is the encode boundary's responsibility (`encode_for_device`
        with `encode_angle`) so every element (bg + mask + text) ends up with
        the same rotation count.  Preview consumers wrap this with
        `_apply_for_preview` to add a single user-rotation for display.
        """
        self.log.debug("_apply_adjustments: brightness=%d split_mode=%d",
                  self.brightness, self.split_mode)
        if self.brightness >= 100 and not self.split_mode:
            return image
        if self.brightness < 100:
            image = ImageService.apply_brightness(image, self.brightness)
        return self._apply_split_overlay(image)

    def _apply_for_preview(self, image: Any) -> Any:
        """Wrap `_apply_adjustments` output with user rotation for GUI preview.

        Encode-bound paths bypass this — they go through
        `encode_for_device(..., encode_angle=self._encode_angle())` which
        is the sole rotator for device bytes.
        """
        rot = self._image_rotation
        if rot:
            image = ImageService.apply_rotation(image, rot)
        return image

    def _apply_split_overlay(self, image: Any) -> Any:
        """Composite Dynamic Island overlay for widescreen split mode."""
        if not self.split_mode or not self.is_widescreen_split:
            return image

        key = (self.split_mode, self.rotation)
        if not (asset_name := SPLIT_OVERLAY_MAP.get(key)):
            return image

        overlay = self._split_overlay_cache.get(key)
        if overlay is None:
            overlay = self._load_split_overlay(asset_name)
            self._split_overlay_cache[key] = overlay

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
            self.log.error("Split overlay composite failed: %s", e)
            return image

    @staticmethod
    def _load_split_overlay(asset_name: str) -> Any | None:
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

    def set_video_fit_mode(self, mode: str) -> Any | None:
        """Set video fit mode. Re-decodes frames. Returns preview image."""
        if self.media.set_fit_mode(mode):
            self._convert_media_frames()
            frame = self.media.get_frame()
            if frame:
                self.current_image = frame
                return self._render_and_process()
        return self._render_and_process()

    # -- Video playback ----------------------------------------------------

    def video_tick(self) -> dict | None:
        """Advance one video frame. Returns dict or None if not playing."""
        frame, should_send, progress = self.media.tick()
        if not frame:
            return None

        self.current_image = frame

        # Cache path: get brightness surface, composite text, encode.
        # Snapshot self._cache to a local — concurrent threads can null
        # self._cache mid-method (theme reload, video unload), and Python
        # attribute reads aren't atomic across multiple ops.
        cache = self._cache
        if cache and cache.active:
            cf = self.media.state.current_frame
            total = self.media.state.total_frames
            index = (cf - 1) % total if total > 0 else 0
            surface = cache.get_surface(index)
            if surface is not None:
                # Composite text overlay (same surface for all frames).
                # Text is composited in source coord space — encode rotates
                # the unified bg+mask+text together so they stay aligned.
                if cache.has_text:
                    r = ImageService.renderer()
                    surface = r.copy_surface(surface)
                    surface = r.composite(surface, cache.text_overlay, (0, 0))
                # Encode + rotate (sole rotator) — read encode_angle fresh
                # from current state so rotation changes apply on next tick
                # without rebuilding the cache.
                device = self.devices.selected
                if device is not None:
                    protocol, resolution, fbl, use_jpeg = device.encoding_params
                    _device_surface, encoded = ImageService.encode_for_device(
                        surface, protocol, resolution, fbl, use_jpeg,
                        encode_angle=self._encode_angle())
                else:
                    encoded = None
            else:
                encoded = None
            return {
                'preview': self._apply_for_preview(surface) if surface is not None else None,
                'frame_index': index,
                'progress': progress,
                'send_image': None,
                'encoded': encoded,
            }

        # Fallback: original pipeline (cache not yet built)
        if self.overlay.enabled:
            frame = self.overlay.render(frame)

        processed = self._apply_adjustments(frame)

        # processed is un-rotated; send_frame encodes rotation via encode_angle.
        # Preview gets a rotated copy so the GUI shows what the device shows.
        result = {'preview': self._apply_for_preview(processed), 'progress': progress,
                  'send_image': processed if (should_send and self.auto_send) else None}

        return result

    def get_video_interval(self) -> int:
        """Get video frame interval in ms for timer setup."""
        return self.media.frame_interval_ms

    def is_video_playing(self) -> bool:
        """Check if video is currently playing."""
        return self.media.is_playing

    # -- Video frame cache -------------------------------------------------

    def _start_video_cache_async(self) -> None:
        """Build video cache in a background thread — zero GUI freeze."""
        import threading
        t = threading.Thread(
            target=self._build_video_cache, daemon=True, name="trcc-cache-build")
        t.start()

    def _build_video_cache(self) -> None:
        """Build L2 cache (mask compositing). Safe to run in a background thread.

        Assigns self._cache atomically on completion so video_tick falls back
        to the uncached path until the cache is ready.
        """
        from .video_cache import VideoFrameCache

        device = self.devices.selected
        if not device:
            self.log.warning("_build_video_cache: no device selected — skipping")
            return
        cache = VideoFrameCache()
        cache.build(
            frames=self.media._frames,
            mask=(self.overlay.theme_mask
                  if self.overlay.enabled and self.overlay.theme_mask_visible
                  else None),
            mask_position=self.overlay.theme_mask_position,
            brightness=self.brightness,
        )
        self._cache = cache  # atomic assignment — GIL keeps this safe
        if self._cpu_percent_fn is not None:
            self.log.info("video cache built: %d frames, trcc CPU %.1f%%",
                     len(self.media._frames), self._cpu_percent_fn())
        else:
            self.log.info("video cache built: %d frames", len(self.media._frames))

    def update_video_cache_text(self, metrics: Any) -> None:
        """Update text overlay in cache once per refresh interval.

        Renders text overlay O(1) and stores it. DisplayService.video_tick()
        composites it onto each frame at tick time — no 147-frame encode loop.
        Snapshot self._cache to a local — concurrent thread can null
        self._cache between the active-check and the call.
        """
        cache = self._cache
        if not (cache and cache.active):
            return
        if self.overlay.enabled:
            surface, key = self.overlay.render_text_only(metrics)
        else:
            surface, key = None, None
        cache.update_text_overlay(surface, key)

    # -- Blocking video loop (CLI / API) ------------------------------------

    def run_video_loop(
        self,
        video_path: Path,
        *,
        overlay_config: dict | None = None,
        mask_path: Path | None = None,
        metrics_fn: Any | None = None,
        on_frame: Any | None = None,
        on_progress: Any | None = None,
        loop: bool = True,
        duration: float = 0,
    ) -> dict:
        """Unified video+overlay pipeline for CLI and API adapters.

        Loads video, sets up overlay (config + mask + metrics polling),
        runs the tick loop, and calls ``on_frame`` per processed frame.

        Args:
            video_path: Video/GIF/ZT file to play.
            overlay_config: Overlay element config dict (from
                ``build_overlay_config()``). Enables overlay if provided.
            mask_path: Mask PNG file or directory. Auto-resized to LCD dims.
            metrics_fn: Callable returning ``HardwareMetrics`` — polled once
                per second for live overlay updates.
            on_frame: Callback ``(processed_image)`` — adapter sends to device.
            on_progress: Callback ``(percent, current_time, total_time)``.
            loop: Whether to loop the video.
            duration: Stop after N seconds (0 = no limit).

        Returns:
            Result dict with success/error/message.
        """
        self.log.info("run_video_loop: path=%s overlay=%s mask=%s loop=%s duration=%s",
                 video_path, bool(overlay_config), bool(mask_path), loop, duration)

        # 1. Load video
        w, h = self.canvas_size
        self.media.set_target_size(w, h)
        if not self.media.load(video_path):
            self.log.error("run_video_loop: failed to load %s", video_path)
            return {"success": False, "error": f"Failed to load: {video_path}"}

        self._convert_media_frames()

        total = self.media.state.total_frames
        fps = self.media.state.fps
        self.log.info("run_video_loop: loaded %d frames, %.0ffps, %dx%d", total, fps, w, h)

        # 2. Set up overlay if config or mask provided
        if overlay_config or mask_path:
            if overlay_config:
                self.log.debug("run_video_loop: overlay config with %d elements", len(overlay_config))
                self.overlay.set_config(overlay_config)
            if mask_path:
                mask_img = OverlayService.load_mask_from_path(
                    Path(mask_path), self.overlay._renderer, w, h)
                if mask_img is not None:
                    self.log.debug("run_video_loop: mask loaded from %s", mask_path)
                    self.overlay.set_theme_mask(mask_img)
            self.overlay.enabled = True

        # 3. Start playback + run tick loop
        self.media._state.loop = loop
        self.media.play()
        return self._run_tick_loop(
            metrics_fn=metrics_fn, on_frame=on_frame,
            on_progress=on_progress, duration=duration)

    def _run_tick_loop(
        self,
        *,
        metrics_fn: Any | None = None,
        on_frame: Any | None = None,
        on_progress: Any | None = None,
        duration: float = 0,
    ) -> dict:
        """Blocking tick loop — shared by run_video_loop and theme-load.

        Assumes media is already loaded + playing, overlay already configured.
        Polls metrics, composites overlay, applies adjustments, calls callbacks.

        Returns:
            Result dict with success/message.
        """
        import time as _time

        total = self.media.state.total_frames
        fps = self.media.state.fps
        interval = self.media.frame_interval_ms / 1000.0
        start = _time.monotonic()
        last_metrics = 0.0

        try:
            while self.media.is_playing:
                frame, should_send, progress = self.media.tick()
                if frame is None:
                    break

                # Poll metrics once per second
                if metrics_fn and self.overlay.enabled:
                    now = _time.monotonic()
                    if now - last_metrics >= 1.0:
                        self.overlay.update_metrics(metrics_fn())
                        last_metrics = now

                # Composite overlay
                if self.overlay.enabled:
                    frame = self.overlay.render(frame)

                # Apply brightness/rotation
                processed = self._apply_adjustments(frame)

                # Send to device
                if on_frame and should_send:
                    on_frame(processed)

                # Progress callback
                if on_progress and progress:
                    on_progress(*progress)

                # Duration limit
                if duration and (_time.monotonic() - start) >= duration:
                    break

                _time.sleep(interval)

        except KeyboardInterrupt:
            return {"success": True, "message": "Stopped",
                    "frames": total, "fps": fps}

        return {"success": True, "message": "Done",
                "frames": total, "fps": fps}

    # -- Blocking static keepalive loop (CLI / API) -------------------------

    def run_static_loop(
        self,
        *,
        interval: float = 0.150,
        duration: float = 0,
        metrics_fn: Any | None = None,
        on_frame: Any | None = None,
    ) -> dict:
        """Re-send current static image at *interval* seconds until interrupted.

        Bulk/LY devices don't retain frames — firmware reverts to the
        built-in logo unless frames keep arriving.  The GUI metrics loop
        handles this automatically; CLI and API call this instead.

        The DeviceService encoding cache makes repeated sends cheap —
        only the USB write happens, no image re-encoding.

        Args:
            interval: Seconds between re-sends (default 150 ms).
            duration: Stop after N seconds (0 = no limit).
            metrics_fn: Callable returning ``HardwareMetrics`` — polled
                once per second for live overlay updates.
            on_frame: Optional callback ``(processed_image)`` per send.

        Returns:
            Result dict with success/message.
        """
        import time as _time

        image = self.current_image
        if not image:
            return {"success": False, "error": "No image loaded"}

        w, h = self.lcd_size
        start = _time.monotonic()
        last_metrics = 0.0

        try:
            while True:
                if metrics_fn and self.overlay.enabled:
                    now = _time.monotonic()
                    if now - last_metrics >= 1.0:
                        self.overlay.update_metrics(metrics_fn())
                        last_metrics = now

                if self.overlay.enabled:
                    frame = self.overlay.render(image)
                else:
                    frame = image
                processed = self._apply_adjustments(frame)
                if on_frame:
                    on_frame(processed)
                self.devices.send_frame(processed, w, h,
                                       encode_angle=self._encode_angle())
                if duration and (_time.monotonic() - start) >= duration:
                    break
                _time.sleep(interval)
        except KeyboardInterrupt:
            return {"success": True, "message": "Stopped"}

        return {"success": True, "message": "Done"}

    # -- LCD send ----------------------------------------------------------

    def send_current_image(self) -> bytes | None:
        """Prepare current image for LCD send. Returns encoded bytes or None."""
        self.log.debug("send_current_image: has_image=%s overlay_enabled=%s",
                  self.current_image is not None, self.overlay.enabled)
        if not self.current_image:
            return None
        image = self.current_image
        if self.overlay.enabled:
            image = self.overlay.render(image)
        image = self._apply_adjustments(image)
        return self._encode_for_device(image)

    def _encode_for_device(self, img: Any) -> bytes:
        """Encode image for LCD device — returns wire bytes only."""
        device = self.devices.selected
        if not device:
            raise RuntimeError("Cannot encode for device — no device selected")
        protocol, resolution, fbl, use_jpeg = device.encoding_params
        _device_surface, frame_bytes = ImageService.encode_for_device(
            img, protocol, resolution, fbl, use_jpeg,
            encode_angle=self._encode_angle())
        return frame_bytes

    # -- Theme save (delegates to ThemePersistence) ------------------------

    def save_theme(self, name: str) -> tuple[bool, str]:
        """Save current config as a custom theme.

        Custom themes always go to user_content_dir (~/.trcc-user/data/) so they
        survive uninstall and data re-downloads.
        """
        if self._path_resolver:
            data_dir = Path(self._path_resolver.user_content_dir()) / 'data'
        elif self._data_dir:
            data_dir = self._data_dir
        else:
            return False, "No data directory configured"
        ok, msg = ThemePersistence.save(
            name, data_dir, self.lcd_size,
            current_image=self._clean_background or self.current_image,
            overlay=self.overlay,
            mask_source_dir=self._mask_source_dir,
            media_source_path=self.media.source_path,
            media_is_playing=self.media.is_playing,
            current_theme_path=self.current_theme_path,
        )
        if ok:
            safe_name = f'Custom_{name}' if not name.startswith('Custom_') else name
            from ..core.paths import theme_dir_name
            self.current_theme_path = data_dir / theme_dir_name(self.lcd_width, self.lcd_height) / safe_name
        return ok, msg

    def export_config(self, export_path: Path) -> tuple[bool, str]:
        """Export current theme as .tr or JSON file."""
        return self._persistence.export_config(
            export_path, self.current_theme_path,
            self.lcd_width, self.lcd_height,
        )

    def import_config(self, import_path: Path, data_dir: Path) -> tuple[bool, str]:
        """Import theme from .tr or JSON file."""
        # Fall back to user-writable dir on system-wide installs (#51)
        if not os.access(data_dir, os.W_OK) and self._path_resolver:
            data_dir = Path(self._path_resolver.data_dir())
        ok, result = self._persistence.import_config(
            import_path, data_dir, self.lcd_size)
        if ok and not isinstance(result, str):
            self.load_local_theme(result)
            return True, f"Imported: {import_path.stem}"
        return ok, result if isinstance(result, str) else "Import failed"

    # -- Directory properties ----------------------------------------------
    # All derived from roots + resolution. Rotation swaps w,h in the name.

    @property
    def theme_dir(self) -> Any | None:
        """Current ThemeDir. Swaps only when portrait themes exist."""
        if not self._data_root:
            return None
        from ..core.models import ThemeDir
        w, h = self.canvas_resolution
        return ThemeDir(str(self._data_root / theme_dir_name(w, h)))

    @property
    def local_dir(self) -> Path | None:
        td = self.theme_dir
        return td.path if td and td.path.exists() else None

    @property
    def web_dir(self) -> Path | None:
        """Active cloud backgrounds dir. Swaps independently on rotation."""
        if not self._data_root:
            return None
        w, h = self._rotated_resolution()
        d = self._data_root / 'web' / web_dir_name(w, h)
        return d if d.exists() else None

    @property
    def masks_dir(self) -> Path | None:
        """Active masks dir. Swaps independently on rotation."""
        if not self._data_root:
            return None
        w, h = self._rotated_resolution()
        d = self._data_root / 'web' / masks_dir_name(w, h)
        return d if d.exists() else None

    @property
    def user_theme_dir(self) -> Path | None:
        """User custom themes dir (~/.trcc-user/data/theme{W}{H})."""
        if not self._user_root:
            return None
        w, h = self.canvas_resolution
        d = self._user_root / theme_dir_name(w, h)
        return d if d.exists() else None

    @property
    def user_masks_dir(self) -> Path | None:
        """User custom masks dir (~/.trcc-user/data/web/zt{W}{H})."""
        if not self._user_root:
            return None
        w, h = self._rotated_resolution()
        d = self._user_root / 'web' / masks_dir_name(w, h)
        return d if d.exists() else None
