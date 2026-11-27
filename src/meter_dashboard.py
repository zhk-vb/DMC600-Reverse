"""DMC-600 desktop dashboard for live BLE readings and capture replay."""

import argparse
import asyncio
import csv
import math
import sys
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, SimpleQueue
from tkinter import filedialog, messagebox, ttk

from bleak import BleakClient, BleakScanner

from meter_locale import translate_message
from meter_ascii import AsciiWriter, DxlWriter, MeasurementTextWriter

from meter_protocol import (
    CMD, DATA, Measurement, MeterSettings, NotificationStream, Reading,
    REMOTE_MODES, control_request, decode_measurement, hello, reported_properties,
    SETTING_PROPERTIES, meter_status_request, query, settings_request,
)

WINDOW = 30
MAX_WINDOW = 3600
RATE_WINDOW = 5
SNAPSHOT_WINDOW = 60
QUERY_INTERVALS_MS = (500, 200)
RECORD_FIELDS = ("t_sec", "len", "hex", "mode", "value", "unit", "range",
                 "state", "hold", "voltage", "voltage_unit", "current", "current_unit")


def record_row(stamp, packet, reading, hold, origin):
    row = dict(t_sec=round(stamp - origin, 6), len=len(packet),
               hex=packet.hex(" ").upper(), hold="" if hold is None else int(hold))
    if reading is not None:
        row.update(mode=reading.mode, unit=reading.unit, range=reading.range_label,
                   state=reading.state)
        if reading.state == "ok":
            row["value"] = format(reading.raw_value, ".9g")
        if channels := reading.power_channels:
            for name, channel in zip(("voltage", "current"), channels):
                row[name + "_unit"] = channel.unit
                if channel.state == "ok":
                    row[name] = format(channel.raw_value, ".9g")
    return row
MODES_ZH = {
    1: "直流电压", 2: "交流电压", 3: "电阻", 4: "电容",
    5: "直流电流", 6: "交流电流", 0x22: "二极管", 0x23: "通断",
    0x24: "频率", 0x25: "温度", 0x28: "NCV", 0x29: "LIVE",
    0x2A: "浪涌电流", 0x2B: "电压电流同测",
}
COLORS = ("#4fe2b6", "#6abcf2", "#ffbe72")


def curve_points(points, start, interval_ms):
    interval = interval_ms / 1000
    last_point = None
    last_frame = None
    for stamp, value in points:
        if stamp < start:
            continue
        if value is None or (last_frame is not None and stamp - last_frame > 2):
            yield stamp, None
            last_point = None
        last_frame = stamp
        if value is not None and (last_point is None or stamp - last_point >= interval - 1e-8):
            yield stamp, value
            last_point = stamp


@dataclass
class Channel:
    reading: Reading
    points: deque = field(default_factory=deque)
    minimum: float | None = None
    maximum: float | None = None

    def add(self, stamp, reading):
        self.reading = reading
        value = reading.raw_value if reading.state == "ok" and math.isfinite(reading.raw_value) else None
        self.points.append((stamp, value))
        while self.points[0][0] < stamp - MAX_WINDOW:
            self.points.popleft()
        if value is not None:
            self.minimum = value if self.minimum is None else min(self.minimum, value)
            self.maximum = value if self.maximum is None else max(self.maximum, value)


class DashboardData:
    def __init__(self):
        self.mode = None
        self.power_modes = None
        self.hold = None
        self.channels = {}
        self.frames = deque()
        self.last = None

    def add(self, stamp: float, reading: Measurement):
        if self.mode != reading.mode_code:
            self.channels.clear()
            self.power_modes = None
        self.mode = reading.mode_code
        extra = reading.power_channels
        if extra:
            modes = tuple(channel.mode_code for channel in extra)
            if self.power_modes is not None and modes != self.power_modes:
                self.channels.clear()
            self.power_modes = modes
        self.last = stamp
        self.frames.append(stamp)
        self.rate(stamp)
        for index, sample in enumerate((reading, *(extra or ()))):
            channel = self.channels.get(index)
            if channel is None or channel.reading.unit_code != sample.unit_code:
                channel = self.channels[index] = Channel(sample)
            channel.add(stamp, sample)

    def rate(self, now):
        while self.frames and self.frames[0] < now - RATE_WINDOW:
            self.frames.popleft()
        if len(self.frames) < 2:
            return 0.0
        return (len(self.frames) - 1) / max(now - self.frames[0], 0.1)


def meter_choices(discovered):
    return {
        f"{name}  |  {device.address}": device.address
        for device, advertisement in discovered.values()
        if (name := (advertisement.local_name or device.name or "").strip()).casefold() == "dmc-600"
    }


async def acquire(address, capture, stop, events, commands=None, *, strict_address=False,
                  query_interval_ms=lambda: QUERY_INTERVALS_MS[0], translate=lambda message: message):
    stream, settings = NotificationStream(), MeterSettings()

    def decode(chunk, stamp):
        for packet in stream.feed(chunk):
            previous = settings.hold
            settings.observe(packet)
            if settings.hold != previous:
                events.put(("hold", settings.hold))
            for prop, value in reported_properties(packet):
                for action, (_, response, _) in SETTING_PROPERTIES.items():
                    if prop == response and len(value) == 1:
                        events.put(("observed", (action, value[0], stamp)))
                if prop == 0x0015 and len(value) == 1:
                    events.put(("observed", ("volume", value[0], stamp)))
                if prop == 0x0016 and len(value) == 1 and value[0] <= 100:
                    events.put(("observed", ("battery", value[0], stamp)))
                if prop == 0x1001 and len(value) == 2:
                    events.put(("observed", ("mode", value[0], stamp)))
                if prop in (0x1005, 0x1006, 0x1008) and value in (b"\x00", b"\x01"):
                    events.put(("observed", ("hold" if prop in (0x1005, 0x1006)
                                             else "relative", value[0], stamp)))
            reading = decode_measurement(packet)
            events.put(("packet", (stamp, packet, reading, settings.hold)))
            if reading is not None:
                events.put(("reading", (stamp, reading)))

    if capture:
        events.put(("status", translate("正在回放本地样本")))
        with capture.open(newline="", encoding="utf-8-sig") as source:
            start = time.monotonic()
            for row in csv.DictReader(source):
                due = start + float(row["t_sec"])
                while time.monotonic() < due and not stop.is_set():
                    await asyncio.sleep(min(0.05, max(0, due - time.monotonic())))
                if stop.is_set():
                    return
                hold = row.get("hold")
                if hold in ("0", "1") and settings.hold != (hold == "1"):
                    settings.hold = hold == "1"
                    events.put(("hold", settings.hold))
                decode(bytes.fromhex(row["hex"]), due)
        events.put(("status", translate("样本回放结束")))
        return

    events.put(("status", translate("正在扫描并连接")))
    discovered = await BleakScanner.discover(timeout=5, return_adv=True)
    events.put(("devices", meter_choices(discovered)))
    if stop.is_set():
        return
    preferred = [d for d, _ in discovered.values() if d.address.casefold() == address.casefold()]
    named = [d for d, a in discovered.values()
             if (a.local_name or d.name or "").strip().casefold() == "dmc-600"]
    if strict_address:
        named = []
    if not preferred and len(named) > 1:
        choices = " / ".join(d.address for d in named)
        raise RuntimeError(translate("发现多台仪表，请从下拉菜单选择：{0}").format(choices))
    if not preferred and not named:
        if strict_address:
            raise RuntimeError(translate("未发现所选设备 {0}，请确认已开机并释放手机连接").format(address))
        raise RuntimeError(translate("未发现万用表，请确认已开机并释放手机连接"))
    device = (preferred or named)[0]
    events.put(("device", device.address))
    options = {"winrt": {"use_cached_services": False}} if sys.platform == "win32" else {}
    async with BleakClient(device, timeout=25, **options) as client:
        await client.start_notify(DATA, lambda _, data: decode(data, time.monotonic()))
        await client.write_gatt_char(CMD, hello(1), response=False)
        await asyncio.sleep(0.6)
        events.put(("status", translate("已连接 · 等待仪表上报")))
        seq = 2
        has_settings = False
        while client.is_connected and not stop.is_set():
            command = None
            if commands is not None:
                try:
                    command = commands.get_nowait()
                except Empty:
                    pass
            if command is None:
                if seq == 3:
                    packet = meter_status_request(seq)
                elif seq >= 5 and not has_settings:
                    packet = settings_request(seq)
                    has_settings = True
                else:
                    packet = query(seq)
            else:
                action, value = command
                events.put(("sent", (action, value, time.monotonic())))
                packet = control_request(seq, action, value)
            await client.write_gatt_char(CMD, packet, response=False)
            seq = seq % 255 + 1
            await asyncio.sleep(query_interval_ms() / 1000)
        if not stop.is_set():
            events.put(("status", translate("蓝牙连接已断开")))


class Dashboard:
    def __init__(self, root, args):
        self.root, self.args = root, args
        language = getattr(args, "language", "zh")
        self.tr = lambda message: translate_message(message, language)
        self.mode_labels = {mode: self.tr(label) for mode, label in MODES_ZH.items()}
        self.events = SimpleQueue()
        self.data = DashboardData()
        self.running = False
        self.commands = SimpleQueue()
        self.pending_control = None
        self.remote_state = {}
        self.live_reading = False
        self.closing = False
        self.stop = threading.Event()
        self.query_interval_ms = QUERY_INTERVALS_MS[0]
        self.devices = {f"DMC-600  |  {args.address}": args.address} if args.address else {}
        self.scanning = False
        self.window_seconds = WINDOW
        self.vertical_limits = None
        self.capture = args.replay
        self.recording_utc_offset = None
        self.history = deque()
        self.record_file = None
        self.record_writer = None
        self.record_origin = None
        self.record_name = None
        self.record_path = None
        self.pending_replay = None
        root.title(self.tr("DMC-600 · 实时仪表盘"))
        root.geometry("1160x780" if language == "en" else "1120x780")
        root.minsize(990 if language == "en" else 850, 700)
        root.configure(bg="#111820")
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TButton", padding=(13, 7), font=("Microsoft YaHei UI", 10),
                        background="#253440", foreground="#eef5f7", bordercolor="#3c5160",
                        lightcolor="#3c5160", darkcolor="#3c5160")
        style.map("TButton", background=[("active", "#374d5c"), ("disabled", "#18242f")],
                  foreground=[("disabled", "#8b9ba4")])
        style.configure("Primary.TButton", background="#4fe2b6", foreground="#10251e",
                        bordercolor="#4fe2b6", lightcolor="#4fe2b6", darkcolor="#4fe2b6")
        style.map("Primary.TButton", background=[("active", "#81eccc"), ("disabled", "#253440")],
                  foreground=[("disabled", "#8b9ba4")])
        style.configure("TCombobox", padding=5, arrowsize=15, background="#344855",
                        arrowcolor="#eef5f7", bordercolor="#3c5160",
                        lightcolor="#3c5160", darkcolor="#3c5160")
        style.map("TCombobox", fieldbackground=[("readonly", "#253440")],
                  foreground=[("readonly", "#eef5f7")])

        header = tk.Frame(root, bg="#111820")
        header.pack(fill="x", padx=24, pady=(20, 8))
        self.label(header, self.tr("DMC-600  /  测量工作台"), 22, "#f1f5ff", bold=True).pack(side="left")
        self.status = tk.StringVar(value=self.tr("未连接"))
        self.label(header, self.status, 10, "#82b8ff").pack(side="right")

        actions = tk.Frame(root, bg="#111820")
        actions.pack(fill="x", padx=24, pady=(0, 12))
        initial_device = f"DMC-600  |  {args.address}" if args.address else self.tr("自动选择 DMC-600")
        self.device_selection = tk.StringVar(value=initial_device)
        self.device_selector = ttk.Combobox(actions, state="readonly", width=34,
                                            textvariable=self.device_selection,
                                            values=(self.tr("自动选择 DMC-600"), *self.devices))
        self.device_selector.pack(side="left")
        self.scan_button = ttk.Button(actions, text=self.tr("扫描设备"), command=self.scan_devices)
        self.scan_button.pack(side="left", padx=(8, 0))
        self.button = ttk.Button(actions, text=self.tr("回放样本") if args.replay else self.tr("连接"),
                                 style="Primary.TButton", command=self.toggle)
        self.button.pack(side="left", padx=8)
        self.telemetry = tk.StringVar(value=self.tr("等待数据"))
        self.label(actions, self.telemetry, 10, "#afc1cc").pack(side="right")

        storage = tk.Frame(root, bg="#111820")
        storage.pack(fill="x", padx=24, pady=(0, 12))
        ttk.Button(storage, text=self.tr("保存最近 60 秒"), command=self.snapshot).pack(side="left")
        self.record_button = ttk.Button(storage, text=self.tr("开始实时保存"), command=self.toggle_record)
        self.record_button.pack(side="left", padx=8)
        self.replay_button = ttk.Button(storage, text=self.tr("打开记录回放"), command=self.open_replay)
        self.replay_button.pack(side="left")
        self.export_format = ttk.Combobox(storage, state="readonly", width=12,
                                         values=("CSV", "ASCII / MDA"))
        self.export_format.current(0)
        self.export_format.pack(side="left", padx=8)
        self.storage_status = tk.StringVar(value="")
        self.label(storage, self.storage_status, 9, "#afc1cc").pack(side="right")

        controls = tk.Frame(root, bg="#111820")
        controls.pack(fill="x", padx=24, pady=(0, 12))
        self.mode_choices = sorted(REMOTE_MODES)
        self.mode_selector = ttk.Combobox(
            controls, state="readonly", width=15,
            values=[self.mode_labels[mode] for mode in self.mode_choices],
        )
        self.mode_selector.current(0)
        self.mode_selector.pack(side="left")
        ttk.Button(controls, text=self.tr("切换档位"), command=self.request_mode).pack(side="left", padx=6)
        ttk.Button(controls, text=self.tr("切换 HOLD"), command=lambda: self.request_toggle("hold")).pack(side="left")
        ttk.Button(controls, text=self.tr("切换 REL/ZERO"),
                   command=lambda: self.request_toggle("relative")).pack(side="left", padx=6)
        ttk.Button(controls, text=self.tr("仪表设置"), command=self.open_settings).pack(side="left")
        self.control_status = tk.StringVar()
        self.label(controls, self.control_status, 9, "#afc1cc").pack(side="right")

        curve = tk.Frame(root, bg="#111820")
        curve.pack(fill="x", padx=24, pady=(0, 8))
        self.label(curve, self.tr("曲线取点间隔"), 10, "#dbe8ff").pack(side="left")
        self.curve_interval = tk.IntVar(value=50)
        self.curve_interval_label = tk.StringVar(value="50 ms")
        tk.Scale(curve, from_=20, to=3000, orient="horizontal", showvalue=False,
                 variable=self.curve_interval, length=210, bg="#111820",
                 troughcolor="#304353", highlightthickness=0,
                 command=lambda value: self.curve_interval_label.set(f"{value} ms")
                 ).pack(side="left", padx=8)
        self.label(curve, self.curve_interval_label, 10, "#dbe8ff").pack(side="left")
        ttk.Button(curve, text=self.tr("坐标范围"), command=self.open_axes).pack(side="left", padx=10)
        query_selector = ttk.Combobox(
            curve, state="readonly", width=8,
            values=[f"{interval} ms" for interval in QUERY_INTERVALS_MS],
        )
        query_selector.current(0)
        query_selector.bind("<<ComboboxSelected>>", self.change_query_interval)
        query_selector.pack(side="right")
        self.label(curve, self.tr("蓝牙查询间隔"), 10, "#dbe8ff").pack(side="right", padx=(0, 8))

        self.canvas = tk.Canvas(root, bg="#18242f", highlightbackground="#304353", highlightthickness=1)
        self.canvas.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        self.label(root, self.tr("曲线显示蓝牙读数趋势  ·  同测档各通道独立绘制"),
                   9, "#afc1cc").pack(anchor="w", padx=24, pady=(0, 16))
        root.after(80, self.refresh)
        if not args.replay and not getattr(args, "manual", False):
            root.after(250, self.auto_start)

    def label(self, parent, text, size, color, bold=False):
        options = {"textvariable": text} if isinstance(text, tk.Variable) else {"text": text}
        return tk.Label(parent, **options, bg="#111820", fg=color,
                        font=("Microsoft YaHei UI", size, "bold" if bold else "normal"))

    def open_axes(self):
        panel = tk.Toplevel(self.root)
        panel.title(self.tr("曲线坐标范围"))
        panel.configure(bg="#111820", padx=20, pady=16)
        panel.transient(self.root)
        panel.resizable(False, False)
        seconds = tk.StringVar(value=f"{self.window_seconds:g}")
        lower, upper = self.vertical_limits or ("", "")
        minimum = tk.StringVar(value=str(lower))
        maximum = tk.StringVar(value=str(upper))
        fields = ((self.tr("横轴跨度（秒，0–3600）"), seconds),
                  (self.tr("纵轴下限（留空自动）"), minimum), (self.tr("纵轴上限（留空自动）"), maximum))
        for row, (title, variable) in enumerate(fields):
            self.label(panel, title, 10, "#eef5f7").grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(panel, textvariable=variable, width=15).grid(row=row, column=1, padx=(14, 0))
        self.label(panel, self.tr("固定纵轴范围应用于各通道的显示单位"), 9, "#afc1cc").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(5, 9))

        def apply():
            try:
                width = float(seconds.get())
                if not 0 < width <= MAX_WINDOW:
                    raise ValueError(self.tr("横轴跨度须大于 0 且不超过 3600 秒"))
                limits = None
                if minimum.get().strip() or maximum.get().strip():
                    lo, hi = float(minimum.get()), float(maximum.get())
                    if not math.isfinite(hi - lo) or not lo < hi:
                        raise ValueError(self.tr("纵轴需要有效的上下限，且下限小于上限"))
                    limits = (lo, hi)
            except ValueError as error:
                messagebox.showerror(self.tr("坐标范围无效"), str(error), parent=panel)
                return
            self.window_seconds, self.vertical_limits = width, limits
            panel.destroy()

        def reset():
            seconds.set(str(WINDOW))
            minimum.set("")
            maximum.set("")
            apply()

        ttk.Button(panel, text=self.tr("恢复默认"), command=reset).grid(row=4, column=0, sticky="w")
        ttk.Button(panel, text=self.tr("应用"), style="Primary.TButton", command=apply).grid(row=4, column=1, sticky="e")
        panel.bind("<Return>", lambda _: apply())
        panel.grab_set()

    def change_query_interval(self, event):
        self.query_interval_ms = QUERY_INTERVALS_MS[event.widget.current()]

    def open_settings(self):
        panel = tk.Toplevel(self.root)
        panel.title(self.tr("DMC-600 仪表设置"))
        panel.transient(self.root)
        labels = {"brightness": self.tr("亮度"), "shutdown": self.tr("自动关机"), "language": self.tr("表端语言")}
        choices = {
            "brightness": {n: f"{n}%" for n in range(10, 101, 10)},
            "shutdown": {255: self.tr("从不"), 5: self.tr("5 分钟"), 15: self.tr("15 分钟"), 30: self.tr("30 分钟")},
            "language": {0: self.tr("简体中文"), 1: "English"},
        }
        status = tk.Label(panel, textvariable=self.control_status, anchor="w")
        status.grid(row=4, column=0, columnspan=4, sticky="ew", padx=12, pady=6)
        current_labels = {}
        for row, (action, options) in enumerate(choices.items()):
            ttk.Label(panel, text=labels[action]).grid(row=row, column=0, padx=12, pady=8)
            current = ttk.Label(panel, text=self.tr("等待仪表"))
            current.grid(row=row, column=1, sticky="w")
            current_labels[action] = current
            values = tuple(options)
            selector = ttk.Combobox(panel, values=tuple(options.values()), width=13,
                                    state="readonly")
            observed = self.remote_state.get(action)
            selector.current(values.index(observed) if observed in options else 0)
            selector.grid(row=row, column=2, padx=8)
            ttk.Button(panel, text=self.tr("应用"),
                       command=lambda key=action, field=selector, keys=values:
                       self.request_control(key, keys[field.current()])).grid(row=row, column=3, padx=8)
        volume = ttk.Label(panel)
        volume.grid(row=3, column=0, columnspan=4, sticky="w", padx=12, pady=4)

        def update():
            if not panel.winfo_exists():
                return
            for action, label in current_labels.items():
                value = self.remote_state.get(action)
                label.configure(text=choices[action].get(value, self.tr("等待仪表") if value is None else str(value)))
            value = self.remote_state.get("volume")
            buzzer = {0: self.tr("关闭"), 80: self.tr("开启"), None: self.tr("未知")}.get(value, str(value))
            volume.configure(text=self.tr("蜂鸣器：{0} · 远程写入待完善").format(buzzer))
            panel.after(300, update)

        update()

    def request_mode(self):
        self.request_control("mode", self.mode_choices[self.mode_selector.current()])

    def request_toggle(self, action):
        current = self.remote_state.get(action)
        if current is None:
            self.control_status.set(self.tr("等待仪表上报状态"))
            return
        self.request_control(action, 1 - current)

    def request_control(self, action, value):
        if not self.live_reading or self.capture or self.stop.is_set():
            self.control_status.set(self.tr("实时连接后可控制"))
            return
        if self.pending_control is not None:
            self.control_status.set(self.tr("等待上一条控制确认"))
            return
        if self.remote_state.get(action) == value:
            self.control_status.set(self.tr("仪表已处于该档位"))
            return
        self.pending_control = (action, value, None)
        self.commands.put((action, value))
        self.control_status.set(self.tr("正在发送控制"))

    def select_device(self, address):
        self.device_selection.set(next(
            (label for label, value in self.devices.items() if value == address),
            self.tr("自动选择 DMC-600"),
        ))

    def scan_devices(self):
        if self.running or self.scanning:
            return
        self.scanning = True
        self.scan_button.configure(state="disabled")
        self.replay_button.configure(state="disabled")
        self.button.configure(state="disabled")
        self.status.set(self.tr("正在扫描附近仪表"))

        def scan():
            try:
                result = asyncio.run(BleakScanner.discover(timeout=5, return_adv=True))
                devices = meter_choices(result)
                self.events.put(("devices", devices))
                status = self.tr("找到 {0} 台 DMC-600").format(len(devices)) if devices else self.tr("未发现 DMC-600")
            except Exception as error:
                status = self.tr("扫描失败 · {0}").format(str(error) or type(error).__name__)
            self.events.put(("scan_done", status))

        threading.Thread(target=scan, daemon=True).start()

    def auto_start(self):
        if not self.running and not self.closing:
            self.toggle()

    def choose_export(self, title):
        ascii_export = self.export_format.current() == 1
        options = {
            "defaultextension": ".ascii" if ascii_export else ".csv",
            "filetypes": [("ASCII", "*.ascii"), ("ETAS DXL ASCII", "*.dxl")] if ascii_export
                         else [("CSV", "*.csv")],
        }
        path = filedialog.asksaveasfilename(parent=self.root, title=title, **options)
        if not path:
            return None
        path = Path(path)
        if path.suffix.lower() not in ((".dxl", ".ascii") if ascii_export else (".csv",)):
            self.storage_status.set(self.tr("文件扩展名与所选格式不符"))
            return None
        return path

    def make_writer(self, target, path):
        if path.suffix.lower() == ".ascii":
            writer = AsciiWriter(target, utc_offset=self.recording_utc_offset)
        elif path.suffix.lower() == ".dxl":
            writer = DxlWriter(target, utc_offset=self.recording_utc_offset)
        else:
            writer = csv.DictWriter(target, fieldnames=RECORD_FIELDS)
        writer.writeheader()
        return writer

    def snapshot(self):
        end = self.history[-1][0] if self.history and not self.running else time.monotonic()
        records = [row for row in self.history if row[0] >= end - SNAPSHOT_WINDOW]
        if not records or not any(row[2] is not None for row in records):
            self.storage_status.set(self.tr("最近 60 秒暂无测量可保存"))
            return
        path = self.choose_export(self.tr("保存最近 60 秒"))
        if path:
            if self.record_path is not None and path.resolve() == self.record_path:
                self.storage_status.set(self.tr("请选择另一个文件，避免覆盖实时记录"))
                return
            try:
                with path.open("w", newline="", encoding="utf-8-sig" if path.suffix.lower() == ".csv" else "ascii") as target:
                    writer = self.make_writer(target, path)
                    for row in records:
                        self.write_record(writer, row, records[0][0])
                self.storage_status.set(self.tr("已保存 {0}").format(path.name))
            except OSError as error:
                messagebox.showerror(self.tr("快照保存失败"), str(error), parent=self.root)

    def toggle_record(self):
        if self.record_file is not None:
            self.finish_record()
            return
        if not self.running or self.capture is not None:
            self.storage_status.set(self.tr("连接仪表后可实时保存"))
            return
        path = self.choose_export(self.tr("保存实时记录"))
        if not path:
            return
        try:
            self.record_file = path.open("w", newline="", encoding="utf-8-sig" if path.suffix.lower() == ".csv" else "ascii")
            self.record_writer = self.make_writer(self.record_file, path)
            self.record_file.flush()
            self.record_origin = time.monotonic()
            self.record_name = path.name
            self.record_path = path.resolve()
            self.record_button.configure(text=self.tr("停止实时保存"))
            self.storage_status.set(self.tr("正在保存 {0}").format(path.name))
        except OSError as error:
            self.finish_record()
            messagebox.showerror(self.tr("实时保存失败"), str(error), parent=self.root)

    @staticmethod
    def write_record(writer, packet, origin):
        if isinstance(writer, MeasurementTextWriter):
            writer.write_measurement(packet[0], packet[2], packet[3], origin)
        else:
            writer.writerow(record_row(*packet, origin))

    def finish_record(self):
        if self.record_file is None:
            return
        try:
            self.record_file.close()
            if self.record_name:
                self.storage_status.set(self.tr("已保存 {0}").format(self.record_name))
        except OSError as error:
            self.storage_status.set(self.tr("保存失败 · {0}").format(error))
        finally:
            self.record_file = self.record_writer = self.record_name = self.record_path = None
            self.record_button.configure(text=self.tr("开始实时保存"))

    def open_replay(self):
        path = filedialog.askopenfilename(parent=self.root, title=self.tr("打开仪表记录"),
                                          filetypes=[("CSV", "*.csv")])
        if path:
            if self.record_path is not None and Path(path).resolve() == self.record_path:
                self.finish_record()
            if self.running:
                self.pending_replay = Path(path)
                self.toggle()
            else:
                self.capture = Path(path)
                self.toggle()

    def toggle(self):
        if self.scanning:
            return
        if self.running:
            self.stop.set()
            self.button.configure(state="disabled")
            return
        self.data = DashboardData()
        self.history.clear()
        self.commands = SimpleQueue()
        self.pending_control = None
        self.remote_state.clear()
        self.live_reading = False
        self.control_status.set("")
        self.stop = threading.Event()
        self.running = True
        self.recording_utc_offset = (
            time.time() - time.monotonic() if self.capture is None else None
        )
        self.status.set(self.tr("正在启动"))
        self.button.configure(text=self.tr("停止回放") if self.capture else self.tr("断开"))
        self.scan_button.configure(state="disabled")
        self.device_selector.configure(state="disabled")
        address = self.devices.get(self.device_selection.get(), "")
        threading.Thread(target=self.worker, args=(address, self.capture, bool(address)),
                         daemon=True).start()

    def worker(self, address, capture, explicit=False):
        async def session():
            async def connection():
                if capture is not None:
                    await acquire(address, capture, self.stop, self.events, self.commands, translate=self.tr)
                    return
                selected = address
                connected_once = False
                delay = 1
                while not self.stop.is_set():
                    # Reuse the selected device through transient dropouts.
                    class SessionEvents:
                        def put(inner, event):
                            nonlocal selected, connected_once, delay
                            if event[0] == "device":
                                selected = event[1]
                            if event[0] == "reading":
                                connected_once = True
                                delay = 1
                            self.events.put(event)

                    try:
                        await acquire(selected, None, self.stop, SessionEvents(), self.commands,
                                      strict_address=explicit or connected_once,
                                      query_interval_ms=lambda: self.query_interval_ms, translate=self.tr)
                    except Exception:
                        if not connected_once:
                            raise
                    if self.stop.is_set() or not connected_once:
                        return
                    self.events.put(("retry", delay))
                    while True:
                        try:
                            self.commands.get_nowait()
                        except Empty:
                            break
                    await asyncio.sleep(delay)
                    delay = min(10, delay * 2)

            task = asyncio.create_task(connection())
            while not task.done() and not self.stop.is_set():
                await asyncio.sleep(0.08)
            if not task.done():
                task.cancel()
            await task
        try:
            asyncio.run(session())
        except asyncio.CancelledError:
            self.events.put(("status", self.tr("已断开")))
        except Exception as error:
            self.events.put(("status", self.tr("读取失败 · {0}").format(str(error) or type(error).__name__)))
        finally:
            self.events.put(("done", None))

    def close(self):
        self.closing = True
        self.stop.set()
        if not self.running:
            self.root.destroy()

    def refresh(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "reading":
                    self.data.add(*payload)
                    if not self.capture:
                        self.live_reading = True
                        self.status.set(self.tr("实时读取"))
                elif kind == "retry":
                    self.live_reading = False
                    self.remote_state.clear()
                    self.data.hold = None
                    if self.pending_control:
                        self.pending_control = None
                        self.control_status.set(self.tr("连接中断，控制未确认"))
                    self.status.set(self.tr("连接中断 · {0} 秒后重连").format(payload))
                elif kind == "sent":
                    self.pending_control = payload
                    self.control_status.set(self.tr("等待仪表确认"))
                elif kind == "observed":
                    action, value, stamp = payload
                    self.remote_state[action] = value
                    if self.pending_control:
                        expected, target, sent = self.pending_control
                        if sent is not None and stamp >= sent and action == expected and value == target:
                            self.pending_control = None
                            self.control_status.set(self.tr("仪表已确认控制"))
                elif kind == "packet":
                    self.history.append(payload)
                    while self.history[0][0] < payload[0] - SNAPSHOT_WINDOW:
                        self.history.popleft()
                    if self.record_writer and payload[0] >= self.record_origin:
                        try:
                            self.write_record(self.record_writer, payload, self.record_origin)
                            self.record_file.flush()
                        except OSError as error:
                            self.finish_record()
                            self.storage_status.set(self.tr("保存失败 · {0}").format(error))
                elif kind == "devices":
                    selected_label = self.device_selection.get()
                    selected = self.devices.get(selected_label)
                    self.devices = payload
                    if selected and selected not in payload.values():
                        self.devices = {selected_label: selected, **payload}
                    self.device_selector.configure(values=(self.tr("自动选择 DMC-600"), *self.devices))
                    self.select_device(selected)
                elif kind == "device":
                    self.select_device(payload)
                elif kind == "scan_done":
                    self.scanning = False
                    self.scan_button.configure(state="normal")
                    self.replay_button.configure(state="normal")
                    self.button.configure(state="normal")
                    self.status.set(payload)
                elif kind == "hold":
                    self.data.hold = payload
                elif kind == "status":
                    self.status.set(payload)
                elif kind == "done":
                    self.running = False
                    self.live_reading = False
                    if self.pending_control:
                        self.pending_control = None
                        self.control_status.set(self.tr("连接结束，控制未确认"))
                    self.finish_record()
                    self.capture = self.args.replay
                    self.scan_button.configure(state="normal")
                    self.device_selector.configure(state="readonly")
                    self.button.configure(state="normal",
                                          text=self.tr("回放样本") if self.capture else self.tr("连接"))
                    if self.closing:
                        self.root.destroy()
                        return
                    if pending := self.pending_replay:
                        self.pending_replay = None
                        self.capture = pending
                        self.toggle()
        except Empty:
            pass
        now = time.monotonic()
        if self.pending_control:
            _, _, sent = self.pending_control
            if sent is not None and now - sent > 4:
                self.pending_control = None
                self.control_status.set(self.tr("仪表未确认，未自动重发"))
        if self.data.last is not None:
            age = now - self.data.last
            rate = self.data.rate(now)
            battery = self.remote_state.get("battery")
            power = "" if battery is None else self.tr("  ·  电量 {0}%").format(battery)
            self.telemetry.set(self.tr("{0:.1f} 帧/s  ·  最近接收 {1:.1f} s 前{2}").format(rate, age, power))
        else:
            self.telemetry.set(self.tr("等待数据"))
        self.draw(now)
        self.root.after(100, self.refresh)

    def draw(self, now):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()

        def text(x, y, value, color="#afc1cc", size=10, anchor="w"):
            c.create_text(x, y, text=value, fill=color, anchor=anchor,
                          font=("Microsoft YaHei UI", size))

        if not self.data.channels:
            text(w / 2, h / 2, self.tr("回放或连接万用表后显示读数"), "#b6c9e4", 15, "center")
            return

        mode = self.data.mode
        name = self.mode_labels.get(mode, self.data.channels[0].reading.mode)
        if self.data.power_modes:
            label = {1: "DC", 2: "AC"}.get(self.data.power_modes[0])
            if label:
                name += f" · {label}"
        hold = {None: self.tr("未知"), True: self.tr("开启"), False: self.tr("关闭")}[self.data.hold]
        relative = {None: self.tr("未知"), 1: self.tr("开启"), 0: self.tr("关闭")}[self.remote_state.get("relative")]
        text(20, 24, f"{name} · 0x{mode:02X} · HOLD {hold} · REL {relative}", "#e4eeff", 13)
        span = self.window_seconds
        scale = self.tr("固定纵轴") if self.vertical_limits else self.tr("自动纵轴")
        text(w - 20, 24, self.tr("最近 {0:g} s  /  {1}").format(span, scale), anchor="e")
        height = (h - 48) / len(self.data.channels)
        end = now if self.running else self.data.last

        for index, channel in self.data.channels.items():
            reading = channel.reading
            color = COLORS[index]
            top = 43 + index * height
            value = format(reading.raw_value, ".7g") if reading.state == "ok" else {
                "overload": "OL", "invalid": "—",
            }[reading.state]
            label = self.mode_labels.get(reading.mode_code, reading.mode)
            c.create_rectangle(14, top + 3, 226, max(top + 112, top + height - 10),
                               fill="#202f3b", outline="#304353")
            c.create_rectangle(14, top + 3, 17, max(top + 112, top + height - 10),
                               fill=color, outline="")
            text(29, top + 23, label, "#dbe8ff")
            text(29, top + 52, f"{value} {reading.unit}", color, 21)
            text(29, top + 79, self.tr("量程 {0}").format(reading.range_label))
            low = "—" if channel.minimum is None else format(channel.minimum, ".6g")
            high = "—" if channel.maximum is None else format(channel.maximum, ".6g")
            text(29, top + 101, f"Min {low}   Max {high}", size=9)
            x0, x1 = 255, w - 82
            y0, y1 = top + 10, max(top + 35, top + height - 23)
            recent = list(curve_points(channel.points, end - span, self.curve_interval.get()))
            values = [v for _, v in recent if v is not None]
            floor, ceiling = self.vertical_limits or ((min(values), max(values)) if values else (0, 1))
            if floor == ceiling:
                pad = max(abs(floor) * 0.02, 0.001)
                floor, ceiling = floor - pad, ceiling + pad
            for step in range(7):
                fraction = step / 6
                x = x0 + (x1 - x0) * fraction
                c.create_line(x, y0, x, y1, fill="#304353")
                text(x, y1 + 11, f"{-span * (1 - fraction):.4g} s" if step < 6 else "0 s",
                     size=8, anchor="center")
            for step in range(3):
                y = y0 + (y1 - y0) * step / 2
                c.create_line(x0, y, x1, y, fill="#304353")
                text(x1 + 6, y, f"{ceiling - (ceiling - floor) * step / 2:.4g}", size=8)
            previous = None
            line = []

            def position(value):
                return y1 - (value - floor) / (ceiling - floor) * (y1 - y0)

            def finish_line():
                if len(line) > 2:
                    c.create_line(*line, fill=color, width=2)
                elif line:
                    x, y = line
                    c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline="")
                line.clear()

            for stamp, value in recent:
                if value is None:
                    finish_line()
                    previous = None
                    continue
                x = x1 - (end - stamp) / span * (x1 - x0)
                inside = floor <= value <= ceiling
                if previous is not None:
                    old_x, old_value = previous
                    old_inside = floor <= old_value <= ceiling
                    if (inside != old_inside
                            or min(value, old_value) < floor < ceiling < max(value, old_value)):
                        # Project only the part of the segment inside the visible range.
                        for bound in (floor, ceiling) if old_value < value else (ceiling, floor):
                            if min(value, old_value) < bound < max(value, old_value):
                                fraction = (bound - old_value) / (value - old_value)
                                line.extend((old_x + fraction * (x - old_x), position(bound)))
                if inside:
                    line.extend((x, position(value)))
                else:
                    finish_line()
                previous = x, value
            finish_line()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="", help="connect to a specific Bluetooth device ID")
    parser.add_argument("--language", choices=("zh", "en"), default="zh", help="dashboard interface language")
    parser.add_argument("--manual", action="store_true", help="skip auto-connect on startup")
    parser.add_argument("--replay", type=Path, metavar="CSV", help="play a raw capture at real speed")
    args = parser.parse_args()
    root = tk.Tk()
    Dashboard(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()
