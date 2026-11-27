# -*- coding: utf-8 -*-
"""项目自检：CRC 算法 + IEEE754 解析 + 协议帧构造。

用法：python tools/verify.py
"""
import argparse
import csv
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from meter_protocol import MeterSettings, NotificationStream, decode_measurement, frame  # noqa: E402


def check_crc() -> bool:
    print("==== CRC16 (Modbus) 自检 ====")
    cases = [
        ("1000015A0500050000000101", "AA551000015A0500050000000101A176"),
        ("1000015A0600050000000101", "AA551000015A0600050000000101B436"),
        ("1000015A0700050000000101", "AA551000015A070005000000010178F7"),
        ("1000015A0800050000000101", "AA551000015A080005000000010138B7"),
    ]
    ok = True
    for body, expect in cases:
        got = frame(bytes.fromhex(body)).hex().upper()
        good = got == expect
        ok = ok and good
        print(f"  {'OK  ' if good else 'FAIL'} {body} -> {got[-4:]} (期望 {expect[-4:]})")
    return ok


def check_float() -> bool:
    print("\n==== IEEE754 大端浮点自检 ====")
    cases = [
        ("416828F6", 14.51),
        ("416A147B", 14.63),
        ("41675C29", 14.46),
        ("416B0A3D", 14.69),
        ("41680000", 14.50),
    ]
    ok = True
    for h, expect in cases:
        v = struct.unpack(">f", bytes.fromhex(h))[0]
        good = abs(v - expect) < 0.02
        ok = ok and good
        print(f"  {'OK  ' if good else 'FAIL'} {h} -> {v:.3f} V (期望 {expect})")
    return ok


def check_notification_stream() -> bool:
    sample = bytes.fromhex("AA551080035A03000E1002000A01010707416828F60202FD45")
    ack = bytes.fromhex("AA5510800100010005000000010185D0")
    bad_crc = sample[:-1] + bytes([sample[-1] ^ 1])
    second = frame(sample[2:17] + bytes.fromhex("416A147B0202"))
    stream = NotificationStream()
    first_chunk = stream.feed(ack + bad_crc + sample[:2])
    second_chunk = stream.feed(sample[2:] + second)
    readings = [decode_measurement(packet) for packet in second_chunk]
    good = (not first_chunk and second_chunk == [sample, second]
            and all(reading is not None and reading.unit == "V"
                    and reading.range_label == "60V" for reading in readings)
            and [round(reading.raw_value, 2) for reading in readings] == [14.51, 14.63])
    multi_property_payload = bytes.fromhex("1001000201071005000101") + sample[9:-2]
    multi_property = frame(sample[2:7] + len(multi_property_payload).to_bytes(2, "big")
                           + multi_property_payload)
    settings = MeterSettings()
    settings.observe(multi_property)
    good = (good and decode_measurement(multi_property) == readings[0]
            and settings.mode_code == 1 and settings.range_code == 7 and settings.hold is True)
    print(f"  {'OK' if good else 'FAIL'} BLE stream: split, packed, multiple properties, invalid CRC")
    return good


def check_capture(capture) -> bool:
    stream = NotificationStream()
    readings = []
    settings = MeterSettings()
    hold_changes = []
    with open(capture, newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            for packet in stream.feed(bytes.fromhex(row["hex"])):
                previous_hold = settings.hold
                settings.observe(packet)
                if settings.hold != previous_hold:
                    hold_changes.append(settings.hold)
                if reading := decode_measurement(packet):
                    readings.append(reading)

    expected = {
        0x01: 2, 0x02: 2, 0x03: 8, 0x04: 9, 0x05: 5, 0x06: 5,
        0x22: 2, 0x23: 6, 0x24: 12, 0x25: 15, 0x28: 0, 0x29: 0,
        0x2A: 5, 0x2B: 17,
    }
    modes_and_units = {(reading.mode_code, reading.unit_code) for reading in readings}
    overload_modes = {reading.mode_code for reading in readings if reading.state == "overload"}
    power_pairs = {
        tuple(channel.mode_code for channel in channels)
        for reading in readings if (channels := reading.power_channels)
    }
    good = (expected.items() <= modes_and_units
            and {0x03, 0x22, 0x23} <= overload_modes
            and {(0x01, 0x05), (0x02, 0x06)} <= power_pairs
            and hold_changes == [False, True, False, True, False]
            and settings.range_code == 0x07)
    print(f"  {'OK' if good else 'FAIL'} Replay fixture: {len(readings)} readings, "
          f"{len({reading.mode_code for reading in readings})} modes, "
          "overload, power channels and HOLD transitions")
    return good


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", default=os.path.join(ROOT, "samples", "demo.csv"))
    args = parser.parse_args()
    r1 = check_crc()
    r2 = check_float()
    r3 = check_notification_stream()
    r4 = check_capture(args.capture)
    print("\n" + "=" * 40)
    print("总体结果:", "PASS" if (r1 and r2 and r3 and r4) else "FAIL")
    sys.exit(0 if (r1 and r2 and r3 and r4) else 1)
