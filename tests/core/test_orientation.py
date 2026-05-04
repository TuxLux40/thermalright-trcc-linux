"""Tests for trcc.core.orientation — Orientation model + standalone helpers."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from trcc.core.orientation import Orientation, output_resolution

# =========================================================================
# output_resolution (standalone function)
# =========================================================================


class TestOutputResolution:
    """output_resolution(w, h, rotation) — swaps dims for non-square at 90/270."""

    # Non-square landscape
    @pytest.mark.parametrize("rot,expected", [
        (0, (1280, 480)),
        (90, (480, 1280)),
        (180, (1280, 480)),
        (270, (480, 1280)),
    ])
    def test_non_square_1280x480(self, rot, expected):
        assert output_resolution(1280, 480, rot) == expected

    @pytest.mark.parametrize("rot,expected", [
        (0, (800, 480)),
        (90, (480, 800)),
        (180, (800, 480)),
        (270, (480, 800)),
    ])
    def test_non_square_800x480(self, rot, expected):
        assert output_resolution(800, 480, rot) == expected

    @pytest.mark.parametrize("rot,expected", [
        (0, (1600, 720)),
        (90, (720, 1600)),
        (180, (1600, 720)),
        (270, (720, 1600)),
    ])
    def test_non_square_1600x720(self, rot, expected):
        assert output_resolution(1600, 720, rot) == expected

    # Square — never swaps
    @pytest.mark.parametrize("rot", [0, 90, 180, 270])
    def test_square_320x320_never_swaps(self, rot):
        assert output_resolution(320, 320, rot) == (320, 320)

    @pytest.mark.parametrize("rot", [0, 90, 180, 270])
    def test_square_480x480_never_swaps(self, rot):
        assert output_resolution(480, 480, rot) == (480, 480)

    @pytest.mark.parametrize("rot", [0, 90, 180, 270])
    def test_square_240x240_never_swaps(self, rot):
        assert output_resolution(240, 240, rot) == (240, 240)

    def test_zero_resolution(self):
        assert output_resolution(0, 0, 90) == (0, 0)


# =========================================================================
# Orientation class
# =========================================================================


class TestOrientationSquare:
    """Orientation on a square device — dirs never swap."""

    def test_square_output_never_swaps(self):
        o = Orientation(320, 320)
        o.rotation = 90
        assert o.output_resolution == (320, 320)

    def test_square_canvas_never_swaps(self):
        o = Orientation(320, 320)
        o.rotation = 90
        assert o.canvas_resolution == (320, 320)

    def test_square_image_rotation_returns_actual(self):
        o = Orientation(320, 320)
        o.rotation = 90
        assert o.image_rotation == 90

    def test_square_is_rotated_false(self):
        o = Orientation(320, 320)
        o.rotation = 90
        assert o.is_rotated() is False


class TestOrientationNonSquare:
    """Orientation on a non-square device — behavior depends on has_portrait_themes."""

    def _make(self, has_portrait: bool = False) -> Orientation:
        o = Orientation(1280, 480)
        o.data_root = Path('/data')
        o.has_portrait_themes = has_portrait
        return o

    # Without portrait themes — pixel rotation
    def test_no_portrait_canvas_stays_landscape(self):
        o = self._make(has_portrait=False)
        o.rotation = 90
        assert o.canvas_resolution == (1280, 480)

    def test_no_portrait_image_rotation_is_actual(self):
        o = self._make(has_portrait=False)
        o.rotation = 90
        assert o.image_rotation == 90

    def test_no_portrait_theme_dir_is_landscape(self):
        o = self._make(has_portrait=False)
        o.rotation = 90
        assert 'theme1280480' in str(o.theme_dir.path)

    # With portrait themes — dir swap
    def test_portrait_canvas_swaps(self):
        o = self._make(has_portrait=True)
        o.rotation = 90
        assert o.canvas_resolution == (480, 1280)

    def test_portrait_image_rotation_is_zero(self):
        o = self._make(has_portrait=True)
        o.rotation = 90
        assert o.image_rotation == 0

    def test_portrait_theme_dir_is_portrait(self):
        o = self._make(has_portrait=True)
        o.rotation = 90
        assert 'theme4801280' in str(o.theme_dir.path)

    def test_output_resolution_always_swaps(self):
        o = self._make(has_portrait=False)
        o.rotation = 90
        assert o.output_resolution == (480, 1280)

    # Web/mask dirs swap independently on rotation
    @patch('pathlib.Path.exists', return_value=True)
    def test_web_dir_swaps_on_rotation(self, _):
        o = self._make(has_portrait=False)
        o.rotation = 90
        assert '4801280' in str(o.web_dir)

    @patch('pathlib.Path.exists', return_value=True)
    def test_masks_dir_swaps_on_rotation(self, _):
        o = self._make(has_portrait=False)
        o.rotation = 90
        assert 'zt4801280' in str(o.masks_dir)

    # At 0° — always landscape
    def test_zero_rotation_uses_landscape(self):
        o = self._make(has_portrait=True)
        o.rotation = 0
        assert 'theme1280480' in str(o.theme_dir.path)

    # User content dirs
    def test_user_theme_dir_from_user_root(self, tmp_path):
        o = self._make()
        o.user_root = tmp_path
        user_td = tmp_path / 'theme1280480'
        user_td.mkdir()
        assert o.user_theme_dir == user_td

    def test_user_theme_dir_none_when_missing(self, tmp_path):
        o = self._make()
        o.user_root = tmp_path
        assert o.user_theme_dir is None

    def test_user_masks_dir_from_user_root(self, tmp_path):
        o = self._make()
        o.user_root = tmp_path
        user_md = tmp_path / 'web' / 'zt1280480'
        user_md.mkdir(parents=True)
        assert o.user_masks_dir == user_md


class TestOrientationToDict:
    """to_dict serializes roots + portrait flag."""

    def test_populated(self):
        o = Orientation(1280, 480)
        o.data_root = Path('/data')
        o.user_root = Path('/user')
        o.has_portrait_themes = True
        d = o.to_dict()
        assert d == {
            'data_root': '/data',
            'user_root': '/user',
            'has_portrait_themes': True,
        }

    def test_none_roots(self):
        o = Orientation(320, 320)
        d = o.to_dict()
        assert d['data_root'] is None
        assert d['user_root'] is None
        assert d['has_portrait_themes'] is False


class TestOrientationFromDict:
    """from_dict restores Orientation from config values."""

    def test_round_trip(self):
        o = Orientation(1280, 480)
        o.data_root = Path('/data')
        o.user_root = Path('/user')
        o.has_portrait_themes = True
        restored = Orientation.from_dict(1280, 480, o.to_dict())
        assert restored is not None
        assert restored.data_root == Path('/data')
        assert restored.user_root == Path('/user')
        assert restored.has_portrait_themes is True

    def test_legacy_format_extracts_data_root(self):
        """Old config format with theme path → extracts data_root from parent."""
        restored = Orientation.from_dict(320, 320, {'theme': '/data/theme320320'})
        assert restored is not None
        assert restored.data_root == Path('/data')

    def test_returns_none_for_malformed(self):
        assert Orientation.from_dict(320, 320, {}) is None
        assert Orientation.from_dict(320, 320, 'bad') is None
