# FNIRSI DMC-600 BLE Dashboard

[简体中文](README.md)

An independent Windows dashboard and command-line recorder for the FNIRSI DMC-600 clamp meter. It reads the meter over Bluetooth LE and displays measurements, ranges, HOLD status, Min/Max and time trends.

## Quick start

Use Windows with Bluetooth LE and Python 3.10 or later with Tk installed. From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python src/meter_dashboard.py --language en
```

Turn on the meter and disconnect it from the phone app. The dashboard auto-connects when it finds one DMC-600. When it finds multiple meters, choose a device by name and Bluetooth ID. To preview the dashboard without a meter:

```powershell
.venv\Scripts\python src/meter_dashboard.py --language en --replay samples/demo.csv
```

## Recording and MDA

Use **Save last 60 s** for a snapshot or **Start recording** for continuous capture. Select CSV for dashboard playback or **ASCII / MDA** for direct ETAS measurement text export (`.ascii` or `.dxl`). Exported voltage, current and power use V, A and W. Timestamps reflect Bluetooth receipt on the PC; use a common event to align with separately recorded ECU data.

The plot defaults to a 30-second window and automatic Y-axis scaling. The BLE query interval defaults to 500 ms and can also be set to 200 ms.

## Verification

```powershell
.venv\Scripts\python tools/verify.py
```

The included trace contains generated measurements for 14 modes. See the [protocol notes](docs/PROTOCOL.md) and [firmware findings](docs/FIRMWARE_V1.0.19.md) for what has been verified on the meter.

[MIT License](LICENSE). This project is independent of FNIRSI and ETAS.
