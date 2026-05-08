"""Contract tests for LinuxPlatform — verifies it fulfils Platform.

All methods are exercised on Linux directly (no mocking needed — the
concrete Linux adapters are available in the test environment).
"""
from __future__ import annotations

from trcc.adapters.system.linux_platform import LinuxPlatform
from trcc.core.ports import Platform, SensorEnumerator


class TestLinuxPlatformIsPlatform:
    def test_is_os_platform(self):
        assert isinstance(LinuxPlatform(), Platform)


class TestLinuxPlatformContract:
    """Platform interface methods exist and return correct types."""

    def setup_method(self):
        self._p = LinuxPlatform()

    def test_create_detect_fn_returns_callable(self):
        assert callable(self._p.create_detect_fn())

    def test_create_sensor_enumerator_returns_sensor_enumerator(self):
        assert isinstance(self._p.create_sensor_enumerator(), SensorEnumerator)

    def test_create_scsi_transport_returns_object(self):
        # Can't create without a real device, but method exists
        assert hasattr(self._p, 'create_scsi_transport')

    def test_get_memory_info_returns_list(self):
        assert isinstance(self._p.get_memory_info(), list)

    def test_get_disk_info_returns_list(self):
        assert isinstance(self._p.get_disk_info(), list)

    def test_autostart_methods_exist(self):
        assert callable(self._p.autostart_enable)
        assert callable(self._p.autostart_disable)
        assert callable(self._p.autostart_enabled)

    def test_config_dir_returns_string(self):
        assert isinstance(self._p.config_dir(), str)

    def test_distro_name_returns_string(self):
        assert isinstance(self._p.distro_name(), str)
        assert len(self._p.distro_name()) > 0


class TestSuspendUsbDevice:
    """LinuxPlatform.suspend_usb_device walks sysfs and writes power nodes."""

    def _make_fake_device(self, root, name, vid, pid):
        dev = root / name
        (dev / 'power').mkdir(parents=True)
        (dev / 'idVendor').write_text(f"{vid:04x}\n")
        (dev / 'idProduct').write_text(f"{pid:04x}\n")
        (dev / 'power' / 'control').write_text('on')
        (dev / 'bConfigurationValue').write_text('1')
        return dev

    def test_returns_false_when_root_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(LinuxPlatform, '_USB_DEVICES_ROOT',
                            str(tmp_path / 'does-not-exist'))
        assert LinuxPlatform().suspend_usb_device(0x0402, 0x3922) is False

    def test_returns_false_when_no_match(self, tmp_path, monkeypatch):
        monkeypatch.setattr(LinuxPlatform, '_USB_DEVICES_ROOT', str(tmp_path))
        self._make_fake_device(tmp_path, '1-1', 0x1234, 0x5678)
        assert LinuxPlatform().suspend_usb_device(0x0402, 0x3922) is False

    def test_writes_autosuspend_and_unconfigure_on_match(self, tmp_path,
                                                         monkeypatch):
        monkeypatch.setattr(LinuxPlatform, '_USB_DEVICES_ROOT', str(tmp_path))
        dev = self._make_fake_device(tmp_path, '1-1', 0x0402, 0x3922)
        assert LinuxPlatform().suspend_usb_device(0x0402, 0x3922) is True
        assert (dev / 'power' / 'control').read_text() == 'auto'
        assert (dev / 'bConfigurationValue').read_text() == '0'

    def test_skips_interface_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(LinuxPlatform, '_USB_DEVICES_ROOT', str(tmp_path))
        # Real interface dirs have a colon (e.g. "1-1:1.0") and no idVendor,
        # so we just need to confirm the loop tolerates them.
        (tmp_path / '1-1:1.0').mkdir()
        self._make_fake_device(tmp_path, '1-1', 0x0402, 0x3922)
        assert LinuxPlatform().suspend_usb_device(0x0402, 0x3922) is True

    def test_suspends_every_match_for_multi_lcd(self, tmp_path, monkeypatch):
        monkeypatch.setattr(LinuxPlatform, '_USB_DEVICES_ROOT', str(tmp_path))
        d1 = self._make_fake_device(tmp_path, '1-1', 0x0402, 0x3922)
        d2 = self._make_fake_device(tmp_path, '2-1', 0x0402, 0x3922)
        assert LinuxPlatform().suspend_usb_device(0x0402, 0x3922) is True
        assert (d1 / 'bConfigurationValue').read_text() == '0'
        assert (d2 / 'bConfigurationValue').read_text() == '0'
