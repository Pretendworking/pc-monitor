# PC Monitor

A standalone PySide6 desktop widget for Windows. It shows live computer readings in a dark, translucent window with a reorderable list.

## Features

- Cyber-tech dashboard with CPU/GPU/RAM/disk cards, a network graph, and a separate apps/sensors column. **Widget view** returns to the narrow reorderable list; both window sizes are remembered.
- CPU/GPU circular gauges show usage and Fahrenheit temperatures. Drag a card's six-dot handle to reorder it; drag its bottom grip for graph height, or right-click the grip for half/full width. Layout changes are saved.
- Control dock includes volume down/up, mute, play/pause, Windows audio-output settings, Task Manager, hardware details, and up to six favorite executable shortcuts. Right-click a favorite to remove it.
- **Settings → Theme** offers Cyan/Violet/Green/Amber accents, edge glow, text size, circular-gauge visibility, and the existing opacity control under Appearance.
- Hardware page reads processor/graphics names, usable RAM, memory modules, physical drives, OS and uptime locally. Slow Windows inventory runs off the interface thread.
- Gaming overlay shows live CPU/GPU/RAM and temperatures above windowed/borderless games; exclusive fullscreen may hide it. Optional PresentMon enables measured game FPS.
- Dashboard status strip distinguishes current resource pressure from sensor-provider availability. **All graphs** changes every time range together; individual cards can still be adjusted separately.
- **Export CSV** saves up to one hour of actual collected samples with UTC timestamps, units, and explicit unavailable values. Export stays local and excludes process lists. History is cleared when the app restarts.
- Click CPU, RAM, disk space or GPU headings to expand details and a history graph. Expanded cards are remembered. Choose 5 minutes, 30 minutes or 1 hour; hover for exact recorded values and sample times.
- Expand Network Speed for download/upload history in Mbps. Changing adapters starts a fresh network history.
- History stays in memory only, starts when the widget opens, and leaves gaps for unavailable samples or pauses longer than 15 seconds.
- **What’s slowing my PC?** opens live resource readings and the top three apps by CPU and RAM. High-use hints describe the latest sample, not a confirmed diagnosis. Disk capacity is kept distinct from disk activity.
- Device-reported warning and critical temperatures are labeled reference limits, separately from current temperatures.
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

The project contains computer monitoring and local Windows controls. It is separate from other desktop widgets.

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

### Optional real FPS capture

Download the [official Intel PresentMon console](https://github.com/GameTechDev/PresentMon/releases/tag/v2.6.0) and place `PresentMon-2.6.0-x64.exe` in `tools/`, or choose it inside **Gaming overlay**. The binary is not included in Git. Open a game, select its process, and click **Start game FPS capture**. Capture may require starting PC Monitor with Windows administrator approval or existing Performance Log Users membership; the app does not change permissions or auto-elevate.

FPS comes from PresentMon frame intervals for the selected process's busiest swap chain over the last two seconds. Missing/stopped captures show N/A; the screen's refresh rate is never substituted. Closing the overlay or the main app stops its capture process. Hardware readings work without PresentMon.

No game installed? Use **Run graphics FPS test** in Gaming overlay. It opens a small animated Direct3D scene and measures its actual presentation intervals; its animation timer is never used as an FPS reading. Search/select a game's rendering process when testing a real game.

If Windows denies the trace, **Restart with Windows approval** offers a one-session administrator restart and opens the test automatically after approval. The entire app (including apps launched from its dock) then runs with administrator access until closed. Declining or canceling leaves the normal instance open. No Windows group, scheduled task, startup setting or persistent permission is changed.

### Hardware readings

- **Network Quality:** in-app quick download/upload estimates via Cloudflare (up to 25 MB), ping/jitter/ICMP loss checks, latency history, adapter link speed, Stop and Copy Results. Optional quality monitoring runs every 30 seconds; speed tests run only when clicked. Blocked ICMP can look like packet loss.
- **Gaming overlay:** mean/peak frame-time history and 1% low FPS from the slowest 1% of recent frames (30 seconds, up to 4,000 frames; at least 100 required). The built-in graphics test measures its own rendered frames without requiring PresentMon.
- **Themes:** NASA Mission Control, Matrix, Amber CRT and Deep Space are available in Settings → Theme.
- **Custom alerts:** enable thresholds, sustained-high duration and optional sound in Settings → Alerts. Alerts appear in the app; repeated notifications are limited to once per metric every five minutes.
- **Movable panels:** Layout offers freely movable/resizable panels, saved positions, Arrange, Keep Together and Reset. Disk Activity is in the dashboard's right column.

- Network speed is current traffic on the selected PC adapter. It is not a speed test, the connection's maximum speed, or whole-home/router usage. Virtual adapters can count the same traffic again, so select the intended adapter when needed.
- Mbps means megabits per second; MB/s means megabytes per second. 8 Mbps equals 1 MB/s.
- Disk space is used capacity; disk activity is combined I/O across all disks.
- GPU usage is the busiest exposed GPU Core/D3D engine on the selected adapter; memory fullness is excluded. A truly idle GPU can show 0%.
- CPU frequency uses exposed core clocks when available; the Windows fallback can be a nominal reported frequency.
- Temperatures are not averaged. Fans & Temperatures shows all discovered fan/temperature sensors; missing or older-than-two-second samples show N/A. Warning/critical reference limits are excluded from live readings. CPU/GPU gauges can retain brief gaps with a last-reading label. Temperature history preserves unavailable samples as gaps.
- Top-app CPU percentages are normalized to total processor capacity. Protected processes may be inaccessible.

## Local settings and privacy

Settings and window/list positions are saved beside the script in `pc_monitor_settings.json`. These files, environments, logs, shortcuts and compiled executables are excluded from Git. The app reads a local sensor service and Windows counters; it does not upload hardware readings or process lists. User-triggered network tests contact Cloudflare or Google; optional quality monitoring repeats ICMP probes. Speed tests transfer generated data to/from Cloudflare and expose your connection's public IP to that service.

## Checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests cover sensor parsing, percentages and units, network adapter selection and counter resets, process sampling, settings migration and list reordering. GUI checks use Qt's offscreen mode. Hardware-specific readings still depend on the actual computer and installed drivers.

## License

[MIT](LICENSE). Dependency and optional sensor-tool licenses apply separately.
