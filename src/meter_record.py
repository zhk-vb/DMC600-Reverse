"""Record DMC-600 measurements directly over BLE."""
import argparse
import asyncio
import csv
import datetime as dt
import math
import sys
from contextlib import ExitStack
from pathlib import Path

from bleak import BleakClient, BleakError, BleakScanner

from meter_protocol import (
    CMD, DATA, MeterSettings, NotificationStream, Reading, decode_measurement, hello, query,
)


def csv_value(reading: Reading) -> str:
    return format(reading.raw_value, ".9g") if reading.state == "ok" else ""


async def record(args: argparse.Namespace) -> int:
    if args.address:
        device = await BleakScanner.find_device_by_address(args.address, timeout=10)
    else:
        discovered = await BleakScanner.discover(timeout=5, return_adv=True)
        meters = [device for device, advertisement in discovered.values()
                  if (advertisement.local_name or device.name or "").strip().casefold() == "dmc-600"]
        if len(meters) > 1:
            print("Multiple DMC-600 meters found. Set --address to one of: "
                  + ", ".join(device.address for device in meters), file=sys.stderr)
            return 1
        device = meters[0] if meters else None
    if device is None:
        print(f"Meter not found: {args.address or 'DMC-600'}", file=sys.stderr)
        return 1
    client_options = {"winrt": {"use_cached_services": False}} if sys.platform == "win32" else {}
    with ExitStack() as files:
        raw = None
        if args.raw:
            raw_file = files.enter_context(args.raw.open("x", newline="", encoding="utf-8"))
            raw = csv.writer(raw_file)
            raw.writerow(("t_sec", "len", "hex"))

        stream = NotificationStream()
        settings = MeterSettings()
        count = 0
        clock = asyncio.get_running_loop().time

        try:
            async with BleakClient(device, timeout=30, **client_options) as client:
                output = files.enter_context(args.output.open("w", newline="", encoding="utf-8-sig"))
                rows = csv.writer(output)
                rows.writerow(("t_sec", "time", "value", "unit", "range", "mode",
                               "state", "mode_code", "secondary_mode_code",
                               "range_code", "unit_code", "auxiliary", "extra",
                               "power_voltage", "power_voltage_unit",
                               "power_current", "power_current_unit", "hold",
                               "configured_mode_code", "configured_range_code"))
                output.flush()
                start = clock()

                def on_rx(_sender, data):
                    nonlocal count
                    elapsed = clock() - start
                    when = dt.datetime.now().isoformat(timespec="milliseconds").split("T")[1]
                    if raw is not None:
                        raw.writerow((round(elapsed, 3), len(data), data.hex(" ").upper()))
                        raw_file.flush()
                    for packet in stream.feed(data):
                        settings.observe(packet)
                        reading = decode_measurement(packet)
                        if reading is None:
                            continue
                        value = csv_value(reading)
                        channel_fields = ("", "", "", "")
                        if channels := reading.power_channels:
                            voltage, current = channels
                            channel_fields = (csv_value(voltage), voltage.unit,
                                              csv_value(current), current.unit)
                        rows.writerow((
                            round(elapsed, 2), when, value, reading.unit, reading.range_label,
                            reading.mode, reading.state, f"0x{reading.mode_code:02X}",
                            f"0x{reading.secondary_mode_code:02X}", f"0x{reading.range_code:02X}",
                            f"0x{reading.unit_code:02X}", reading.auxiliary.hex(" "),
                            reading.extra.hex(" "), *channel_fields,
                            "" if settings.hold is None else int(settings.hold),
                            "" if settings.mode_code is None else f"0x{settings.mode_code:02X}",
                            "" if settings.range_code is None else f"0x{settings.range_code:02X}",
                        ))
                        count += 1
                        display = {"overload": "OL", "invalid": "--"}.get(
                            reading.state, format(reading.raw_value, ".7g")
                        )
                        hold = " HOLD" if settings.hold else ""
                        print(f"[{elapsed:7.2f}s] {reading.mode:18s} {display:>12s} "
                              f"{reading.unit}  {reading.range_label}{hold}", flush=True)
                    output.flush()

                await client.start_notify(DATA, on_rx)
                await client.write_gatt_char(CMD, hello(1), response=False)
                await asyncio.sleep(0.6)
                seq = 2
                while clock() - start < args.duration and client.is_connected:
                    await client.write_gatt_char(CMD, query(seq), response=False)
                    seq = seq % 255 + 1
                    await asyncio.sleep(min(0.5, max(0, args.duration - (clock() - start))))
                if clock() - start < args.duration:
                    print("Meter disconnected before recording finished.", file=sys.stderr)
                    return 1
        except (BleakError, OSError, TimeoutError) as error:
            print(f"Recording interrupted: {error or type(error).__name__}", file=sys.stderr)
            return 1
        finally:
            if count:
                print(f"{count} readings saved to {args.output}", flush=True)
        if not count:
            print("No decodable measurement frames received.", file=sys.stderr)
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("duration", nargs="?", type=float, default=60)
    parser.add_argument("output", nargs="?", type=Path, default=Path("meter_log.csv"))
    parser.add_argument("--address", help="Bluetooth device ID (auto-detect a single DMC-600 by default)")
    parser.add_argument("--raw", type=Path, metavar="CSV", help="save raw BLE notifications to a new file")
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration <= 0:
        parser.error("duration must be positive and finite")
    if args.raw and args.raw.resolve() == args.output.resolve():
        parser.error("raw capture and readings need different files")
    try:
        return asyncio.run(record(args))
    except OSError as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
