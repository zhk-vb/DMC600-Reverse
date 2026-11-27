"""Decode DMC-600 notification frames and measurement properties."""

import math
import struct
from collections.abc import Iterator
from dataclasses import dataclass


DATA = "0000ffe4-0000-1000-8000-00805f9b34fb"
CMD = "0000ffe9-0000-1000-8000-00805f9b34fb"

SYNC = bytes.fromhex("AA5510")
ACK_HEADER = bytes.fromhex("10800100")
ACK_PAYLOAD = bytes.fromhex("0000000101")
MEASUREMENT_REPORT = bytes.fromhex("108003")
PROPERTY_REPORT = bytes.fromhex("10A003")
OVERLOAD_LIMIT = 1e30

# FNIRSI+ hT protocol codes. Several entries belong to other meter models.
MODES = {
    0x01: "dc_voltage", 0x02: "ac_voltage", 0x03: "resistance",
    0x04: "capacitance", 0x05: "dc_current", 0x06: "ac_current",
    0x07: "dc_current_ua", 0x08: "ac_current_ua",
    0x09: "dc_current_ma", 0x0A: "ac_current_ma",
    0x10: "dc_current_a", 0x11: "ac_current_a",
    0x12: "dc_voltage_mv", 0x13: "ac_voltage_mv",
    0x14: "dc_voltage_v", 0x15: "ac_voltage_v",
    0x21: "online_resistance", 0x22: "diode", 0x23: "continuity",
    0x24: "frequency", 0x25: "temperature", 0x26: "ac_dc_voltage",
    0x27: "ac_dc_current", 0x28: "ncv", 0x29: "live",
    0x2A: "inrush_current", 0x2B: "power",
    0x2C: "ac_dc_current_ua", 0x2E: "ac_dc_current_ma",
    0x30: "ac_dc_current_a", 0x32: "ac_dc_voltage_mv",
    0x33: "ac_dc_voltage_v", 0x34: "auto", 0x35: "diode_continuity",
}

# BK enum codes used by the app for display and unit normalization.
UNITS = {
    0x01: "mV", 0x02: "V", 0x03: "μA", 0x04: "mA", 0x05: "A",
    0x06: "Ω", 0x07: "kΩ", 0x08: "MΩ",
    0x09: "nF", 0x0A: "μF", 0x0B: "mF",
    0x0C: "Hz", 0x0D: "kHz", 0x0E: "MHz",
    0x0F: "°C", 0x10: "°F", 0x11: "W", 0x12: "kW",
}

DC_VOLTAGE_MODES = {0x01, 0x12, 0x14}
AC_VOLTAGE_MODES = {0x02, 0x13, 0x15}
VOLTAGE_RANGES = {
    0: "600mV", 1: "6V", 2: "60V", 3: "600V", 7: "AUTO", 8: "AUTO+",
}


def crc16(data: bytes) -> int:
    result = 0xFFFF
    for byte in data:
        result ^= byte
        for _ in range(8):
            result = (result >> 1) ^ 0xA001 if result & 1 else result >> 1
    return result


def frame(body: bytes) -> bytes:
    return b"\xAA\x55" + body + crc16(body).to_bytes(2, "big")


def hello(seq: int) -> bytes:
    """握手：告诉设备是 FNIRSI APP，进入实时模式"""
    name = b"FNIRSI"
    body = bytes([0x10, 0x20, 0x02, 0x00, seq & 0xFF, 0x00,
                  0x0A, 0x00, 0x11, 0x00, len(name)]) + name
    return frame(body)


def query(seq: int) -> bytes:
    body = bytes([0x10, 0x20, 0x02, 0x00, seq & 0xFF, 0x00]) + bytes([
        0x10, 0x10, 0x01, 0x00, 0x00,
        0x10, 0x02, 0x00, 0x00,
        0x10, 0x08, 0x00, 0x00,
        0x10, 0x03, 0x00, 0x00,
    ])
    return frame(body)


REMOTE_MODES = frozenset((1, 2, 3, 4, 5, 6, 0x22, 0x23, 0x24, 0x25,
                          0x28, 0x29, 0x2A, 0x2B))


def property_request(seq: int, prop_id: int, value: bytes = b"") -> bytes:
    payload = struct.pack(">HH", prop_id, len(value)) + value
    return frame(bytes((0x10, 0x20, 2, 0, seq & 0xFF))
                 + len(payload).to_bytes(2, "big") + payload)


def meter_status_request(seq: int) -> bytes:
    # The app groups these system properties with the HOLD status read.
    return frame(bytes((0x10, 0x20, 2, 0, seq & 0xFF, 0))
                 + bytes.fromhex("0C 0015 0000 0016 0000 1006 0000"))


SETTING_PROPERTIES = {
    "brightness": (0x000B, 0x0014, tuple(range(0, 101, 10))),
    "shutdown": (0x000D, 0x0019, (255, 5, 15, 30)),
    "language": (0x0018, 0x0017, (0, 1)),
}


def settings_request(seq: int) -> bytes:
    payload = b"".join(struct.pack(">HH", prop, 0)
                       for prop in (0x0014, 0x0015, 0x0017, 0x0019))
    return frame(bytes((0x10, 0x20, 2, 0, seq & 0xFF, 0, len(payload))) + payload)


def control_request(seq: int, action: str, value: int) -> bytes:
    if action in SETTING_PROPERTIES:
        prop, _, allowed = SETTING_PROPERTIES[action]
        if value in allowed:
            return property_request(seq, prop, bytes((value,)))
    if action == "mode" and value in REMOTE_MODES:
        return property_request(seq, 0x1004, bytes((value,)))
    if action in ("hold", "relative") and value in (0, 1):
        return property_request(seq, 0x1005 if action == "hold" else 0x1007, bytes((value,)))
    raise ValueError("Unsupported DMC-600 control")


class NotificationStream:
    """Consume frames across notification boundaries, including packed frames."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buffer.extend(chunk)
        frames = []
        while True:
            offset = self.buffer.find(SYNC)
            if offset < 0:
                # Preserve a possible partial sync marker across notifications.
                keep = 2 if self.buffer.endswith(SYNC[:2]) else int(self.buffer.endswith(SYNC[:1]))
                del self.buffer[:len(self.buffer) - keep]
                return frames
            del self.buffer[:offset]
            if len(self.buffer) < 9:
                return frames
            size = 11 + int.from_bytes(self.buffer[7:9], "big")
            if self.buffer[3] not in (0x80, 0xA0) or size > 4096:
                del self.buffer[0]
                continue
            if len(self.buffer) < size:
                return frames
            packet = bytes(self.buffer[:size])
            if packet[2:6] == ACK_HEADER and packet[9:-2] == ACK_PAYLOAD:
                # Live ACK trailers differ from the CRC used for property frames.
                del self.buffer[:size]
                continue
            if crc16(packet[2:-2]) != int.from_bytes(packet[-2:], "big"):
                del self.buffer[0]
                continue
            frames.append(packet)
            del self.buffer[:size]


@dataclass(frozen=True)
class Reading:
    mode_code: int
    raw_value: float
    range_code: int
    unit_code: int

    @property
    def mode(self) -> str:
        return MODES.get(self.mode_code, f"0x{self.mode_code:02X}")

    @property
    def unit(self) -> str:
        return UNITS.get(self.unit_code, "")

    @property
    def range_label(self) -> str:
        raw_code = f"0x{self.range_code:02X}"
        if self.mode_code not in DC_VOLTAGE_MODES | AC_VOLTAGE_MODES:
            return raw_code
        if self.range_code == 4:
            return "1000V" if self.mode_code in DC_VOLTAGE_MODES else "750V"
        return VOLTAGE_RANGES.get(self.range_code, raw_code)

    @property
    def state(self) -> str:
        if math.isnan(self.raw_value):
            return "invalid"
        if abs(self.raw_value) > OVERLOAD_LIMIT:
            return "overload"
        return "ok"


@dataclass(frozen=True)
class Measurement(Reading):
    secondary_mode_code: int
    auxiliary: bytes
    extra: bytes

    @property
    def power_channels(self) -> tuple[Reading, Reading] | None:
        if self.mode_code != 0x2B or len(self.extra) != 14:
            return None
        return _decode_channel(self.extra, 0), _decode_channel(self.extra, 7)


def _decode_channel(data: bytes, offset: int) -> Reading:
    return Reading(*struct.unpack_from(">BfBB", data, offset))


def reported_properties(packet: bytes) -> Iterator[tuple[int, bytes]]:
    if packet[2:5] not in (MEASUREMENT_REPORT, PROPERTY_REPORT):
        return
    payload = packet[9:-2]
    offset = 0
    while offset + 4 <= len(payload):
        prop_id, size = struct.unpack_from(">HH", payload, offset)
        start = offset + 4
        offset = start + size
        if offset > len(payload):
            return
        yield prop_id, payload[start:offset]


def decode_measurement(packet: bytes) -> Measurement | None:
    """Decode the primary 0x1002 record from a validated response frame."""
    if packet[2:5] != MEASUREMENT_REPORT:
        return None
    for prop_id, value in reported_properties(packet):
        if prop_id == 0x1002 and len(value) >= 10:
            return Measurement(
                value[0],
                struct.unpack_from(">f", value, 4)[0],
                value[8],
                value[9],
                value[1],
                value[2:4],
                value[10:],
            )
    return None


@dataclass
class MeterSettings:
    mode_code: int | None = None
    range_code: int | None = None
    hold: bool | None = None

    def observe(self, packet: bytes) -> None:
        for prop_id, value in reported_properties(packet):
            if prop_id == 0x1001 and len(value) == 2:
                self.mode_code, self.range_code = value
            if prop_id in (0x1005, 0x1006) and value in (b"\x00", b"\x01"):
                self.hold = bool(value[0])
