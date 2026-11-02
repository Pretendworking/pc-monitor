# PC Monitor

A standalone PySide6 desktop widget for Windows. It shows live computer readings in a dark, translucent window with a reorderable list.

## Features

- CPU and RAM usage, disk space and GPU usage.
- Blue usage bars below 50%, yellow from 50% to below 80%, red at 80% and above.
- Fahrenheit hardware temperatures when supported, displayed without averaging.
- Network download/upload on a selected adapter in Mbps or MB/s. Automatic selection excludes loopback and common virtual adapters.
- Combined disk read/write rates, logical CPU usage bars, CPU frequency, GPU memory and clocks.
- Battery status, exposed fan/temperature sensors, and top apps by CPU and RAM.
- Checkboxes to choose which monitors appear, expandable sections and saved list order.
- Drag the blue six-dot handles to reorder items. Drag the window background to move it; drag the bottom-right grip to resize.
- Saved settings, transparency, compact view, position lock and always-on-top.
- Single instance per project folder. Reopening the launcher focuses the existing window.

The project contains only computer monitoring. It is separate from any other desktop widgets.

## Run on Windows

Install Python 3.10 or newer, then open PowerShell in this folder:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe pc_monitor.py
```

Tested with Python 3.15, PySide6 6.12.0 and psutil 7.2.2. Other compatible versions are allowed by `requirements.txt`.

Click **Settings** to configure the widget. Close it with **×** or Escape.

### Optional Windows launcher

Build the launcher using Windows' .NET Framework C# compiler:

```powershell
.\scripts\build_launcher.ps1
```

This creates `PC Monitor Launcher.exe` alongside the Python file. Create a desktop shortcut to that file using File Explorer. The launcher uses this project's `.venv`, so it does not depend on another project or a user-specific folder.

After building, **Settings → Start with Windows** can enable or disable startup for the current user. The entry points to this project folder; update the setting if the folder moves.

## Hardware sensors

CPU/RAM usage, network and disk counters use [psutil](https://psutil.readthedocs.io/stable/). Battery availability depends on the computer.

For additional GPU data and hardware temperatures/fans, run [Libre Hardware Monitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor). The widget can use its local web server at `http://127.0.0.1:8085/data.json` or its WMI provider. Enable **Options → Remote Web Server → Run** for the web connection. WMI can be queried through Windows without extra Python packages; optional `WMI` and `pywin32` packages provide another connection path.

Libre Hardware Monitor must remain running for its readings. To keep it out of the way, use its **Start Minimized**, **Minimize To Tray** and **Run On Windows Startup** options. When available, the launcher invokes Libre's configured `LibreHardwareMonitor` startup task instead of directly requesting elevation on every launch. The sensor application is not bundled.

NVIDIA cards can also provide readings through the installed driver's [nvidia-smi](https://docs.nvidia.com/deploy/nvidia-smi/) utility.

Unavailable readings show **N/A**. No temperatures or GPU activity are fabricated.

## Reading the numbers

- Network speed is current traffic on the selected PC adapter. It is not a speed test, the connection's maximum speed, or whole-home/router usage. Virtual adapters can count the same traffic again, so select the intended adapter when needed.
- Mbps means megabits per second; MB/s means megabytes per second. 8 Mbps equals 1 MB/s.
- Disk space is used capacity; disk activity is combined I/O across all disks.
- GPU usage is the busiest exposed GPU Core/D3D engine on the selected adapter; memory fullness is excluded. A truly idle GPU can show 0%.
- CPU frequency uses exposed core clocks when available; the Windows fallback can be a nominal reported frequency.
- Temperatures are not averaged. Brief sensor gaps retain the last reading for up to five seconds, with older samples labeled as last readings.
- Top-app CPU percentages are normalized to total processor capacity. Protected processes may be inaccessible.

## Local settings and privacy

Settings and window/list positions are saved beside the script in `pc_monitor_settings.json`. These files, environments, logs, shortcuts and compiled executables are excluded from Git. The app reads a local sensor service and Windows counters; it does not upload readings or process lists.

## Checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests cover sensor parsing, percentages and units, network adapter selection and counter resets, process sampling, settings migration and list reordering. GUI checks use Qt's offscreen mode. Hardware-specific readings still depend on the actual computer and installed drivers.

## License

[MIT](LICENSE). Dependency and optional sensor-tool licenses apply separately.
