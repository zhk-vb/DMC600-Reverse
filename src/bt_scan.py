# -*- coding: utf-8 -*-
"""扫描附近 BLE 设备。用法: python bt_scan.py [扫描秒数]"""
import asyncio
import sys
from bleak import BleakScanner


async def main(dur: float):
    print(f"扫描 BLE 设备 {dur:.0f} 秒（请让万用表开机并靠近电脑）...\n")
    items = []
    try:
        result = await BleakScanner.discover(timeout=dur, return_adv=True)
        items = [(d.address, d.name or "", adv.rssi) for d, adv in result.values()]
    except TypeError:
        devs = await BleakScanner.discover(timeout=dur)
        items = [(d.address, d.name or "", getattr(d, "rssi", 0) or 0) for d in devs]

    if not items:
        print("没有发现任何 BLE 设备。")
        print("可能原因: 万用表未开机 / 已被手机 APP 占用 / 其实是经典蓝牙(SPP)。")
        return

    for addr, name, rssi in sorted(items, key=lambda x: -(x[2] or -999)):
        print(f"{addr:<40} rssi={rssi:>4}  name={name!r}")
    print(f"\n共 {len(items)} 个设备")


if __name__ == "__main__":
    d = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    asyncio.run(main(d))
