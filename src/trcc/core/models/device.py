"""Device models — entries, registries, detection, device info, button images."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .protocol import _DEFAULT_PROFILE, BULK_RGB565_FBLS, DeviceProfile, get_profile

# Default sidebar button images — matches C# fallback for unknown PM.
LCD_DEFAULT_BUTTON = "A1CZTV"
LED_DEFAULT_BUTTON = "A1KVMALEDC6"


@dataclass(frozen=True, slots=True)
class DeviceEntry:
    """Registry entry describing a known USB device's capabilities."""
    vendor: str
    product: str
    implementation: str
    model: str = "CZTV"
    button_image: str = LCD_DEFAULT_BUTTON
    protocol: str = "scsi"
    device_type: int = 1  # 1=SCSI, 2=HID Type 2, 3=HID Type 3, 4=Raw USB Bulk
    fbl: int = 100         # FBL code (resolution identifier) — used by Windows SCSI poll fallback


@dataclass(frozen=True, slots=True)
class UsbAddress:
    """Bus + address — a unique physical USB device location.

    Two coolers with identical VID:PID still have distinct (bus, address)
    pairs, so threading this through the transport open path lets each
    protocol bind to its own physical device (issue #128).
    """
    bus: int
    address: int

    @classmethod
    def parse(cls, usb_path: str) -> UsbAddress | None:
        """Parse 'usb:5:2' into UsbAddress(5, 2). Returns None on bad input."""
        match usb_path.split(':'):
            case ['usb', bus, addr]:
                try:
                    return cls(int(bus), int(addr))
                except ValueError:
                    return None
        return None

    def matches(self, dev: Any) -> bool:
        """pyusb `custom_match` — bind only to this physical device."""
        return dev.bus == self.bus and dev.address == self.address

    def __str__(self) -> str:
        return f"usb:{self.bus}:{self.address}"


@dataclass(slots=True)
class DetectedDevice:
    """Detected USB/SCSI device."""
    vid: int  # Vendor ID
    pid: int  # Product ID
    vendor_name: str
    product_name: str
    usb_path: str  # e.g., "usb:5:2"
    scsi_device: str | None = None  # e.g., "/dev/sg0"
    implementation: str = "generic"  # Device-specific implementation
    model: str = "CZTV"  # Device model for button image lookup
    button_image: str = LCD_DEFAULT_BUTTON  # Sidebar image prefix
    protocol: str = "scsi"  # "scsi" or "hid"
    device_type: int = 1  # 1=SCSI, 2=HID Type 2, 3=HID Type 3, 4=Bulk, 5=LY

    @property
    def path(self) -> str:
        """Device path for protocol factories (SCSI → /dev/sgN, else USB path)."""
        return self.scsi_device or self.usb_path

    @property
    def addr(self) -> UsbAddress | None:
        """Physical USB (bus, address) parsed from usb_path. None for SCSI-only devices."""
        return UsbAddress.parse(self.usb_path)


# =========================================================================
# Device registries — single source of truth for all known USB devices
# =========================================================================

SCSI_DEVICES: dict[tuple[int, int], DeviceEntry] = {
    (0x87CD, 0x70DB): DeviceEntry(
        vendor="Thermalright", product="LCD Display",
        implementation="thermalright_lcd_v1",
    ),
    (0x0416, 0x5406): DeviceEntry(
        vendor="Winbond", product="LCD Display",
        implementation="ali_corp_lcd_v1",
    ),
    (0x0402, 0x3922): DeviceEntry(
        vendor="Thermalright", product="LCD Display",
        model="FROZEN_WARFRAME", button_image=LCD_DEFAULT_BUTTON,
        implementation="ali_corp_lcd_v1",
    ),
}

HID_LCD_DEVICES: dict[tuple[int, int], DeviceEntry] = {
    (0x0416, 0x5302): DeviceEntry(
        vendor="Winbond", product="USBDISPLAY",
        implementation="hid_type2", protocol="hid", device_type=2,
    ),
    (0x0418, 0x5303): DeviceEntry(
        vendor="ALi Corp", product="LCD Display",
        implementation="hid_type3", protocol="hid", device_type=3,
    ),
    (0x0418, 0x5304): DeviceEntry(
        vendor="ALi Corp", product="LCD Display",
        implementation="hid_type3", protocol="hid", device_type=3,
    ),
}

LED_DEVICES: dict[tuple[int, int], DeviceEntry] = {
    (0x0416, 0x8001): DeviceEntry(
        vendor="Winbond", product="LED Controller",
        model="LED_DIGITAL", implementation="hid_led",
        protocol="led", device_type=1,
        button_image=LED_DEFAULT_BUTTON,
    ),
}

BULK_DEVICES: dict[tuple[int, int], DeviceEntry] = {
    (0x87AD, 0x70DB): DeviceEntry(
        vendor="ChiZhu Tech", product="GrandVision 360 AIO",
        model="GRAND_VISION", button_image="A1GRAND VISION",
        implementation="bulk_usblcdnew",
        protocol="bulk", device_type=4,
    ),
}

LY_DEVICES: dict[tuple[int, int], DeviceEntry] = {
    (0x0416, 0x5408): DeviceEntry(
        vendor="Winbond", product="Trofeo Vision 9.16 LCD",
        implementation="ly_bulk", protocol="ly", device_type=5,
    ),
    (0x0416, 0x5409): DeviceEntry(
        vendor="Winbond", product="Trofeo Vision 9.16 LCD",
        implementation="ly_bulk", protocol="ly", device_type=5,
    ),
}

ALL_DEVICES: dict[tuple[int, int], DeviceEntry] = {
    **SCSI_DEVICES, **HID_LCD_DEVICES, **LED_DEVICES, **BULK_DEVICES, **LY_DEVICES,
}


@dataclass(slots=True)
class DeviceInfo:
    """
    Information about a connected LCD device.

    Matches Windows FormCZTV device data.
    """
    name: str
    path: str  # /dev/sgX
    resolution: tuple[int, int] = (0, 0)  # Discovered via handshake

    # Device properties (from detection)
    vendor: str | None = None
    product: str | None = None
    model: str | None = None
    vid: int = 0
    pid: int = 0
    addr: UsbAddress | None = None  # Physical USB (bus, address) for non-SCSI; None for SCSI
    device_index: int = 0  # 0-based ordinal among detected devices
    fbl_code: int | None = None  # Resolution identifier
    protocol: str = "scsi"  # "scsi" or "hid"
    device_type: int = 1  # 1=SCSI, 2=HID Type 2 ("H"), 3=HID Type 3 ("ALi")
    implementation: str = "generic"  # e.g. "thermalright_lcd_v1", "hid_type2", "hid_led"
    button_image: str = LCD_DEFAULT_BUTTON    # Sidebar image prefix
    pm_byte: int = 0                # Raw PM from handshake (for button image lookup)
    sub_byte: int = 0               # Raw SUB from handshake (for encode rotation lookup)
    led_style_id: int | None = None  # LED style from probe (avoids name-based lookup)
    led_style_sub: int = 0              # LED style sub-variant (C# nowLedStyleSub)

    # State
    connected: bool = True
    brightness: int = 65  # 0-100% (C# default: 65)
    rotation: int = 0  # 0, 90, 180, 270

    @classmethod
    def from_dict(cls, d: dict) -> DeviceInfo:
        """Create DeviceInfo from a detection dict (find_lcd_devices output)."""
        return cls(
            name=d.get('name', 'LCD'),
            path=d.get('path', ''),
            resolution=d.get('resolution', (0, 0)),
            vendor=d.get('vendor'),
            product=d.get('product'),
            model=d.get('model'),
            vid=d.get('vid', 0),
            pid=d.get('pid', 0),
            addr=UsbAddress.parse(d.get('path', '')),
            device_index=d.get('device_index', 0),
            protocol=d.get('protocol', 'scsi'),
            device_type=d.get('device_type', 1),
            implementation=d.get('implementation', 'generic'),
            led_style_id=d.get('led_style_id'),
            led_style_sub=d.get('led_style_sub', 0),
        )

    def to_wire_dict(self) -> dict:
        """Serialize for IPC transport.

        ``dataclasses.asdict`` recursively dictifies nested dataclasses
        (notably ``addr: UsbAddress``), and tuples become lists — both
        round-trip cleanly through JSON.  Pair with ``from_wire_dict``.
        """
        import dataclasses
        return dataclasses.asdict(self)

    @classmethod
    def from_wire_dict(cls, d: dict) -> DeviceInfo:
        """Reconstruct from a ``to_wire_dict`` payload.

        Restores the nested ``UsbAddress`` dataclass and converts the
        ``resolution`` list back to a tuple — JSON has no tuple type.
        """
        addr_dict = d.get('addr')
        addr = UsbAddress(**addr_dict) if addr_dict else None
        res = d.get('resolution', (0, 0))
        return cls(
            name=d.get('name', ''),
            path=d.get('path', ''),
            resolution=(res[0], res[1]) if isinstance(res, list | tuple) else (0, 0),
            vendor=d.get('vendor'),
            product=d.get('product'),
            model=d.get('model'),
            vid=d.get('vid', 0),
            pid=d.get('pid', 0),
            addr=addr,
            device_index=d.get('device_index', 0),
            fbl_code=d.get('fbl_code'),
            protocol=d.get('protocol', 'scsi'),
            device_type=d.get('device_type', 1),
            implementation=d.get('implementation', 'generic'),
            button_image=d.get('button_image', LCD_DEFAULT_BUTTON),
            pm_byte=d.get('pm_byte', 0),
            sub_byte=d.get('sub_byte', 0),
            led_style_id=d.get('led_style_id'),
            led_style_sub=d.get('led_style_sub', 0),
            connected=d.get('connected', True),
            brightness=d.get('brightness', 65),
            rotation=d.get('rotation', 0),
        )

    @classmethod
    def from_detected(cls, d: DetectedDevice, device_index: int = 0) -> DeviceInfo:
        """Create DeviceInfo from a DetectedDevice."""
        return cls(
            name=f"{d.vendor_name} {d.product_name}",
            path=d.path,
            vendor=d.vendor_name,
            product=d.product_name,
            model=d.model,
            vid=d.vid,
            pid=d.pid,
            addr=d.addr,
            device_index=device_index,
            protocol=d.protocol,
            device_type=d.device_type,
            implementation=d.implementation,
            button_image=d.button_image,
        )

    @property
    def resolution_str(self) -> str:
        """Get resolution as string (e.g., '320x320')."""
        return f"{self.resolution[0]}x{self.resolution[1]}"

    @property
    def profile(self) -> DeviceProfile:
        """Device profile derived from FBL code."""
        return get_profile(self.fbl_code) if self.fbl_code is not None else _DEFAULT_PROFILE

    @property
    def use_jpeg(self) -> bool:
        """Whether this device uses JPEG encoding.

        Bulk/LY: JPEG unless FBL is RGB565-only (e.g. FBL 100).
        HID: JPEG if profile says so. SCSI: always RGB565.
        """
        if self.protocol in ('bulk', 'ly'):
            return self.fbl_code not in BULK_RGB565_FBLS
        return self.profile.jpeg if self.protocol == 'hid' else False

    @property
    def encoding_params(self) -> tuple:
        """Encoding params for ImageService.encode_for_device().

        Returns (protocol, resolution, fbl, use_jpeg).
        """
        res = self.resolution
        fbl = self.fbl_code
        if res == (0, 0):
            res = self.profile.resolution
        return (self.protocol, res, fbl, self.use_jpeg)


# Implementation key → display name (SCSI LCD devices)
IMPL_NAMES: dict[str, str] = {
    "thermalright_lcd_v1": "Thermalright LCD v1 (USBLCD)",
    "ali_corp_lcd_v1": "ALi Corp LCD v1 (USBLCD)",
    "generic": "Generic LCD",
}


# =============================================================================
# Device Button Image Map (from UCDevice.cs ADDUserButton)
# =============================================================================

_LCD_BUTTON_IMAGE: dict[int, dict[int | None, str]] = {
    1:   {0: 'A1GRAND VISION', 1: 'A1GRAND VISION',
          48: 'A1LM22', 49: 'A1LF14', None: 'A1GRAND VISION'},
    3:   {None: 'A1CORE VISION'},
    4:   {1: 'A1HYPER VISION', 2: 'A1RP130 VISION', 3: 'A1LM16SE',
          4: 'A1LF10V', 5: 'A1LM19SE'},
    5:   {None: 'A1Mjolnir VISION'},
    6:   {1: 'frozen_warframe_ultra', 2: 'A1FROZEN VISION V2'},
    7:   {1: 'A1Stream Vision', 2: 'A1Mjolnir VISION PRO'},
    9:   {0: 'A1LC2JD', 1: 'A1LC2JD', 2: 'A1LC2JD', 3: 'A1LC2JD',
          4: 'A1LC2JD', None: 'A1LF19'},
    10:  {5: 'A1LF16', 6: 'A1LF18', 7: 'A1LD6', None: 'A1LC3'},
    11:  {6: 'A1LD8', None: 'A1LF19'},
    12:  {None: 'A1LF167'},
    13:  {None: 'A1PC1'},
    14:  {1: 'A1Stream Vision', 2: 'A1Mjolnir VISION PRO'},
    15:  {2: 'A1LC8', None: 'A1LC7'},
    16:  {None: 'A1CZ2'},
    17:  {1: 'A1PC1', 2: 'A1LC9', 5: 'A1PC1', None: 'A1PC1'},
    32:  {0: 'A1ELITE VISION', 1: 'A1FROZEN WARFRAME PRO',
          None: 'A1ELITE VISION'},
    36:  {None: 'A1AS120 VISION'},
    49:  {None: 'A1FROZEN WARFRAME'},
    50:  {None: 'A1FROZEN WARFRAME'},
    51:  {None: 'A1FROZEN WARFRAME'},
    52:  {None: 'A1BA120 VISION'},
    53:  {1: 'A1LF21', 2: 'A1LF22', None: 'A1LF20'},
    54:  {None: 'A1LC5'},
    58:  {0: 'A1FROZEN WARFRAME SE', None: 'A1LM26'},
    63:  {0: 'A1FROZEN WARFRAME PRO', 1: 'A1LM22', 2: 'A1LM27',
          3: 'A1LM30'},
    64:  {0: 'A1FROZEN WARFRAME PRO', 1: 'A1LM22', 2: 'A1LM27',
          3: 'A1LM30'},
    65:  {0: 'A1ELITE VISION', 1: 'A1LF14', 2: 'A1LF14', 3: 'A1LD7',
          4: 'A1LD10', 5: 'A1LD7'},
    66:  {0: 'A1ELITE VISION', 1: 'A1LF14', 2: 'A1LF14',
          3: 'A1LD7', 4: 'A1LD7'},
    68:  {None: 'A1LM24'},
    69:  {2: 'A1LD9'},
    100: {0: 'A1FROZEN WARFRAME PRO', 1: 'A1LM22',
          None: 'A1FROZEN WARFRAME PRO'},
    101: {0: 'A1ELITE VISION', 1: 'A1LF14', None: 'A1ELITE VISION'},
    128: {None: 'A1LM24'},
    129: {None: 'A1GRAND VISION'},
}

_LED_BUTTON_IMAGE: dict[int, dict[int | None, str]] = {
    1:   {None: 'A1FROZEN HORIZON PRO'},
    2:   {None: 'A1FROZEN MAGIC PRO'},
    3:   {None: 'A1AX120 DIGITAL'},
    16:  {None: 'A1PA120 DIGITAL'},
    23:  {None: 'A1RK120 DIGITAL'},
    32:  {None: 'A1AK120 Digital'},
    48:  {None: 'A1LF8'},
    49:  {None: 'A1LF10'},
    80:  {None: 'A1LF12'},
    96:  {None: 'A1LF10'},
    112: {None: 'A1LC2'},
    128: {None: 'A1LC1'},
    129: {None: 'A1LF11'},
    144: {None: 'A1LF15'},
    160: {None: 'A1LF13'},
    176: {None: 'A1LF25'},
    208: {None: 'A1CZ1'},
    **{pm: {None: 'A1PA120 DIGITAL'} for pm in range(17, 32) if pm != 23},
}


def _resolve_button(table: dict[int, dict[int | None, str]],
                     key: int, sub: int) -> str | None:
    match table.get(key):
        case None:
            return None
        case sub_map if sub in sub_map:
            return sub_map[sub]
        case sub_map:
            return sub_map.get(None)


def get_button_image(key: int, sub: int = 0, *, is_led: bool = False) -> str | None:
    """Resolve device button image from PM+SUB (HID) or VID+PID (SCSI).

    Args:
        key: PM byte (HID) or VID (SCSI).
        sub: SUB byte (HID) or PID (SCSI).
        is_led: True for LED devices (C# case 1), False for LCD (cases 2-4, 257).
    """
    return _resolve_button(_LED_BUTTON_IMAGE if is_led else _LCD_BUTTON_IMAGE, key, sub)


__all__ = [
    'ALL_DEVICES',
    'BULK_DEVICES',
    'HID_LCD_DEVICES',
    'IMPL_NAMES',
    'LCD_DEFAULT_BUTTON',
    'LED_DEFAULT_BUTTON',
    'LED_DEVICES',
    'LY_DEVICES',
    'SCSI_DEVICES',
    'DetectedDevice',
    'DeviceEntry',
    'DeviceInfo',
    'UsbAddress',
    'get_button_image',
]
