"""ETAS single-rate ASCII and multirate DXL measurement exports."""

import csv
import math

# Each signal keeps a fixed unit across modes and autorange changes.
UNITS = {
    "mV": ("Voltage_V", 1e-3), "V": ("Voltage_V", 1),
    "μA": ("Current_A", 1e-6), "mA": ("Current_A", 1e-3), "A": ("Current_A", 1),
    "Ω": ("Resistance_Ohm", 1), "kΩ": ("Resistance_Ohm", 1e3),
    "MΩ": ("Resistance_Ohm", 1e6),
    "nF": ("Capacitance_F", 1e-9), "μF": ("Capacitance_F", 1e-6),
    "mF": ("Capacitance_F", 1e-3),
    "Hz": ("Frequency_Hz", 1), "kHz": ("Frequency_Hz", 1e3),
    "MHz": ("Frequency_Hz", 1e6),
    "°C": ("Temperature_degC", 1), "°F": ("Temperature_degF", 1),
    "W": ("Power_W", 1), "kW": ("Power_W", 1e3),
}
SIGNALS = tuple(dict.fromkeys(name for name, _ in UNITS.values())) + (
    "Mode", "Status", "Hold", "UTC_s",
)
STATUS = {"ok": 0, "overload": 1, "invalid": 2}


SIGNAL_UNITS = {
    "Voltage_V": "V", "Current_A": "A", "Resistance_Ohm": "Ohm",
    "Capacitance_F": "F", "Frequency_Hz": "Hz", "Temperature_degC": "degC",
    "Temperature_degF": "degF", "Power_W": "W", "UTC_s": "s",
}


class MeasurementTextWriter:
    def __init__(self, target, *, utc_offset=None):
        self.target = target
        self.writer = csv.writer(target, delimiter="\t")
        self.utc_offset = utc_offset
        self.signals = SIGNALS if utc_offset is not None else SIGNALS[:-1]

    def write_measurement(self, stamp, reading, hold, origin):
        if reading is None:
            return
        instant = format(stamp - origin, ".9f")
        values = {"Mode": reading.mode_code, "Status": STATUS[reading.state]}
        if self.utc_offset is not None:
            values["UTC_s"] = format(stamp + self.utc_offset, ".6f")
        if hold is not None:
            values["Hold"] = int(hold)
        for channel in (reading, *(reading.power_channels or ())):
            if channel.state == "ok" and math.isfinite(channel.raw_value) and channel.unit in UNITS:
                signal, factor = UNITS[channel.unit]
                values[signal] = format(channel.raw_value * factor, ".12g")
        self.write_sample(instant, values)


class DxlWriter(MeasurementTextWriter):
    def writeheader(self):
        self.writer.writerow(["time", *(field for name in self.signals
                                       for field in ("time", f"DMC600_{name}"))])

    def write_sample(self, instant, values):
        self.writer.writerow([instant, *(field for name in self.signals
                                         for field in ((instant, values[name])
                                                       if name in values else ("", "")))])


class AsciiWriter(MeasurementTextWriter):
    def writeheader(self):
        self.target.write("ETASAsciiItemFile\trecord\tCrLf\tTab\r\n")
        self.target.write("sampleCount\t")
        self.count_offset = self.target.tell()
        self.count = 0
        self.target.write(f"{self.count:<20}\r\n")
        self.writer.writerow(["time", *(f"DMC600_{name}\\DMC600:1" for name in self.signals)])
        self.writer.writerow(["f8"] * (1 + len(self.signals)))
        units = ("s", *(SIGNAL_UNITS.get(name, "") for name in self.signals))
        self.target.write("\t".join(f'"{unit}"' for unit in units) + "\r\n")

    def write_sample(self, instant, values):
        self.writer.writerow([instant, *(values.get(name, "NaN") for name in self.signals)])
        self.count += 1
        end = self.target.tell()
        self.target.seek(self.count_offset)
        self.target.write(f"{self.count:<20}")
        self.target.seek(end)
