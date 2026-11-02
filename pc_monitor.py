"""Standalone desktop monitor. Install: pip install PySide6 psutil

Optional Windows sensors: pip install WMI pywin32, then run Libre Hardware
Monitor with WMI enabled. NVIDIA GPUs also use nvidia-smi when available.
Disk percentage means system-drive space used, not disk activity.
"""
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import re
import urllib.request
from pathlib import Path

import psutil
from PySide6.QtCore import Qt, QTimer, QLockFile, QMimeData
from PySide6.QtGui import QColor, QPainter, QPen, QIcon, QDrag
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget, QDialog,
                               QFormLayout, QSpinBox, QCheckBox, QComboBox,
                               QDialogButtonBox, QMessageBox, QScrollArea,
                               QToolButton, QFrame, QTabWidget)

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'pc_monitor_settings.json'
DEFAULTS = dict(refresh=1, opacity=90, width=360, height=0, top=False, locked=False,
                compact=False, temperatures=True, start_windows=False, drive='', x=None, y=None,
                expand_network=True, expand_activity=False, expand_cpu=False, expand_gpu=False,
                expand_battery=False, expand_hardware=False, expand_apps=False)
MONITORS = [('cpu', 'CPU usage and temperature'), ('ram', 'RAM usage'),
            ('disk', 'Disk space'), ('gpu', 'GPU usage and temperature'),
            ('network', 'Network download and upload speed'), ('activity', 'Disk read and write speed'),
            ('cores', 'CPU clock and logical cores'),
            ('graphics', 'GPU memory and clocks'), ('battery', 'Battery status'),
            ('hardware', 'Fans and other temperatures'), ('apps', 'Top apps by CPU and RAM')]
DEFAULTS.update({'show_' + key: True for key, _ in MONITORS})
DEFAULTS.update(separate_panels=False, panel_positions={}, monitor_order=[key for key, _ in MONITORS])
DEFAULTS.update(network_adapter='', network_unit='Mbps')
def set_windows_startup(enabled):
    """Called only when the user saves a changed Start with Windows setting."""
    if os.name != 'nt':
        return
    import winreg
    launcher = ROOT / 'PC Monitor Launcher.exe'
    if enabled and not launcher.is_file():
        raise OSError('The PC Monitor Launcher.exe file is missing from the widget folder.')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
        if enabled:
            winreg.SetValueEx(key, 'PCMonitorWidget', 0, winreg.REG_SZ, f'"{launcher}"')
        else:
            try:
                winreg.DeleteValue(key, 'PCMonitorWidget')
            except FileNotFoundError:
                pass


def load_settings():
    result = dict(DEFAULTS)
    result['panel_positions'] = {}
    try:
        saved = json.loads(CONFIG.read_text(encoding='utf-8'))
        if not isinstance(saved, dict):
            return result
        for key in result:
            if key in saved and type(saved[key]) is type(result[key]):
                result[key] = saved[key]
        for key in ('x', 'y'):
            if type(saved.get(key)) is int:
                result[key] = saved[key]
        for key, low, high in [('refresh', 1, 10), ('opacity', 35, 100), ('width', 320, 1200), ('height', 0, 1600)]:
            result[key] = max(low, min(high, result[key]))
    except (OSError, ValueError):
        pass
    # This widget uses one reorderable list; migrate the previous floating layout.
    result['separate_panels'] = False
    order = result.get('monitor_order', [])
    valid = [key for key, _ in MONITORS]
    result['monitor_order'] = list(dict.fromkeys(key for key in order if isinstance(key, str) and key in valid))
    result['monitor_order'].extend(key for key in valid if key not in result['monitor_order'])
    return result


class SettingsDialog(QDialog):
    def __init__(self, monitor):
        super().__init__(monitor)
        self.setWindowTitle('PC Monitor settings')
        self.setMinimumWidth(360)
        self.setStyleSheet('QDialog {background: #151e2c; color: #e6edf8;}'
                          'QLabel,QCheckBox {color: #e6edf8; font-size: 13px;}'
                          'QSpinBox,QComboBox {background: #273449; color: white; padding: 6px;}'
                          'QPushButton {background: #293e59; color: white; padding: 8px 14px; border-radius: 5px;}')
        layout = QVBoxLayout(self)
        heading = QLabel('Make it yours')
        heading.setStyleSheet('font-size: 20px; font-weight: 600;')
        layout.addWidget(heading)
        tabs = QTabWidget()
        tabs.setStyleSheet('QTabWidget::pane {border: none;} QTabBar::tab {background: #273449; color: white; padding: 8px;} QTabBar::tab:selected {background: #345679;}')
        appearance = QWidget()
        appearance_layout = QVBoxLayout(appearance)
        form = QFormLayout()
        self.controls = {}
        for key, title, low, high, suffix in [
                ('refresh', 'Refresh every', 1, 10, ' sec'),
                ('opacity', 'Background opacity', 35, 100, '%'),
                ('width', 'Widget width', 320, 1200, ' px')]:
            control = QSpinBox()
            control.setRange(low, high)
            control.setSuffix(suffix)
            control.setValue(monitor.options[key])
            self.controls[key] = control
            form.addRow(title, control)
        self.drive = QComboBox()
        drives = {monitor.drive}
        try:
            drives.update(p.mountpoint for p in psutil.disk_partitions() if 'cdrom' not in p.opts)
        except OSError:
            pass
        self.drive.addItems(sorted(drives))
        self.drive.setCurrentText(monitor.drive)
        form.addRow('Disk to monitor', self.drive)
        self.network_adapter = QComboBox()
        self.network_adapter.addItem('Automatic · active Wi-Fi / Ethernet', '')
        try:
            adapters = sorted(psutil.net_io_counters(pernic=True))
        except (OSError, psutil.Error):
            adapters = []
        saved_adapter = monitor.options['network_adapter']
        if saved_adapter and saved_adapter not in adapters:
            adapters.append(saved_adapter)
        for name in adapters:
            self.network_adapter.addItem(name, name)
        self.network_adapter.setCurrentIndex(max(0, self.network_adapter.findData(saved_adapter)))
        form.addRow('Network adapter', self.network_adapter)
        self.network_unit = QComboBox()
        self.network_unit.addItem('Mbps · megabits per second', 'Mbps')
        self.network_unit.addItem('MB/s · megabytes per second', 'MB/s')
        self.network_unit.setCurrentIndex(max(0, self.network_unit.findData(monitor.options['network_unit'])))
        form.addRow('Network units', self.network_unit)
        for key, title in [('top', 'Always on top'), ('locked', 'Lock window position'),
                           ('compact', 'Compact view'), ('temperatures', 'Show Fahrenheit temperatures'),
                           ('start_windows', 'Start with Windows')]:
            control = QCheckBox(title)
            control.setChecked(monitor.options[key])
            self.controls[key] = control
            form.addRow(control)
        appearance_layout.addLayout(form)
        tabs.addTab(appearance, 'Appearance')
        monitoring = QWidget()
        monitoring_layout = QVBoxLayout(monitoring)
        for key, title in MONITORS:
            control = QCheckBox(title)
            control.setChecked(monitor.options['show_' + key])
            self.controls['show_' + key] = control
            monitoring_layout.addWidget(control)
        actions = QHBoxLayout()
        for text, checked in [('Select all', True), ('Clear all', False)]:
            button = QPushButton(text)
            button.clicked.connect(lambda _, value=checked: [self.controls['show_' + key].setChecked(value) for key, _ in MONITORS])
            actions.addWidget(button)
        monitoring_layout.addLayout(actions)
        monitoring_layout.addStretch()
        tabs.addTab(monitoring, 'Monitoring')
        layout.addWidget(tabs)
        note = QLabel('Colors: blue below 50% · yellow 50–79.9% · red 80%+\n'
                      'Unavailable hardware sensors display N/A.')
        note.setWordWrap(True)
        note.setStyleSheet('color: #9cabbd; font-size: 11px;')
        layout.addWidget(note)
        sensor_help = QLabel('<a style="color:#7ac2ff" href="https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases">Get Libre Hardware Monitor for GPU temperatures</a>')
        sensor_help.setWordWrap(True)
        sensor_help.setOpenExternalLinks(True)
        sensor_help.setToolTip('In Libre Hardware Monitor: Options → Remote Web Server → Run. Default port: 8085. Leave it running.')
        layout.addWidget(sensor_help)
        connection_note = QLabel('In Libre: enable Run On Windows Startup, Start Minimized,\n'
                                 'and Minimize To Tray once for background temperatures.\n'
                                 'For web sensors: Options → Remote Web Server → Run (port 8085).')
        connection_note.setWordWrap(True)
        connection_note.setStyleSheet('color: #9cabbd; font-size: 11px;')
        layout.addWidget(connection_note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                  QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        result = {k: c.isChecked() if isinstance(c, QCheckBox) else c.value()
                  for k, c in self.controls.items()}
        result['drive'] = self.drive.currentText()
        result['network_adapter'] = self.network_adapter.currentData()
        result['network_unit'] = self.network_unit.currentData()
        return result


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def color(value):
    return '#45aaff' if value < 50 else '#ffd166' if value < 80 else '#ff6577'


def fahrenheit(value):
    return 'N/A' if value is None else f'{value * 1.8 + 32:.1f} °F'


def parse_lhm_tree(tree):
    """Read the local web server's sensor tree, keeping GPU fields paired."""
    cpus, gpus, clocks, inventory = [], {}, [], []
    stack = [(tree, '')]
    while stack:
        node, hardware = stack.pop()
        if not isinstance(node, dict):
            continue
        if node.get('HardwareId'):
            hardware = str(node.get('Text', ''))
        stack.extend((child, hardware) for child in node.get('Children', []))
        ident = str(node.get('SensorId', '')).lower()
        if not ident:
            continue
        value = number(node.get('RawValue'))
        if value is None:
            text = str(node.get('Value', ''))
            match = re.search(r'[-+]?\d+(?:[.,]\d+)?', text)
            value = number(match.group().replace(',', '.')) if match else None
            if value is not None and '/temperature/' in ident and 'f' in text.lower():
                value = (value - 32) / 1.8
        if value is None:
            continue
        name = str(node.get('Text', ''))
        parts = ident.rsplit('/', 2)
        device, kind = parts[:2] if len(parts) == 3 else ('', '')
        if not hardware:
            friendly = next((label for token, label in [('gpu-', 'GPU'), ('cpu', 'CPU'),
                             ('nvme', 'Drive'), ('hdd', 'Drive'), ('ssd', 'Drive'),
                             ('lpc', 'Motherboard'), ('memory', 'Memory')]
                             if token in device), 'Hardware')
        inventory.append(dict(id=ident, name=name, device=hardware or friendly,
                              kind=kind, value=value))
        if '/temperature/' in ident and ('/amdcpu/' in ident or '/intelcpu/' in ident):
            cpus.append(value)
        if kind == 'clock' and ('/amdcpu/' in ident or '/intelcpu/' in ident) and 'core' in name.lower():
            clocks.append(value)
        if '/gpu-' in ident:
            gpu = gpus.setdefault(device, {})
            if '/temperature/' in ident:
                if 'gpu_temp' not in gpu or name.lower() == 'gpu core':
                    gpu['gpu_temp'] = value
                    gpu['gpu_temp_name'] = name
            elif '/load/' in ident and (name.lower() == 'gpu core' or name.lower().startswith('d3d')):
                if value >= gpu.get('gpu', -1):
                    gpu['gpu'] = max(0, min(100, value))
                    gpu['gpu_engine_name'] = name
    result = {'cpu_temp': max(cpus)} if cpus else {}
    result['hardware'] = inventory
    if clocks:
        result['cpu_clock'] = sum(clocks) / len(clocks)
    for key in sorted(gpus):
        if gpus[key]:
            result.update(gpus[key])
            result['gpu_device'] = key
            break
    return result


def parse_sensor_records(records):
    if isinstance(records, dict):
        records = [records]
    return parse_lhm_tree({'Children': [
        {'SensorId': s.get('Identifier', ''), 'Text': s.get('Name', ''),
         'RawValue': s.get('Value')} for s in (records or []) if isinstance(s, dict)]})


def byte_rate(value):
    if value is None:
        return 'N/A'
    for unit in ('B/s', 'KiB/s', 'MiB/s', 'GiB/s'):
        if value < 1024 or unit == 'GiB/s':
            return f'{value:.1f} {unit}'
        value /= 1024


def counter_rate(current, previous, seconds):
    if current is None or previous is None or seconds <= 0 or current < previous:
        return None
    return (current - previous) / seconds


def network_rate(bytes_per_second, unit='Mbps'):
    if bytes_per_second is None:
        return 'N/A'
    return (f'{bytes_per_second / 1_000_000:.1f} MB/s' if unit == 'MB/s'
            else f'{bytes_per_second * 8 / 1_000_000:.1f} Mbps')


def choose_network_adapter(counters, stats, requested='', previous=''):
    if requested:
        return requested if requested in counters and stats.get(requested) and stats[requested].isup else ''
    excluded = ('loopback', 'bluetooth', 'virtual', 'vethernet', 'vmware', 'virtualbox',
                'local area connection*', 'teredo', 'tunnel', 'vpn', 'tailscale', 'wireguard', 'tap-')
    candidates = [name for name in counters if stats.get(name) and stats[name].isup
                  and not any(token in name.lower() for token in excluded)]
    if previous in candidates:
        return previous
    return max(candidates, key=lambda name: counters[name].bytes_recv + counters[name].bytes_sent,
               default='')


class NetworkSampler:
    def __init__(self):
        self.adapter = ''
        self.previous = None
        self.last = None
        self.last_request = None
        self.last_time = None

    def sample(self, requested=''):
        if (self.last_time is not None and self.last_request == requested
                and time.monotonic() - self.last_time < 0.5):
            return dict(self.last)
        try:
            stats = psutil.net_if_stats()
            counters = psutil.net_io_counters(pernic=True)
        except (OSError, psutil.Error):
            stats, counters = {}, {}
        now = time.monotonic()
        adapter = choose_network_adapter(counters, stats, requested, self.adapter)
        record = counters.get(adapter)
        result = dict(adapter=adapter or requested, download=None, upload=None)
        if record and self.previous and adapter == self.adapter and self.last_request == requested:
            elapsed = now - self.last_time
            result['download'] = counter_rate(record.bytes_recv, self.previous.bytes_recv, elapsed)
            result['upload'] = counter_rate(record.bytes_sent, self.previous.bytes_sent, elapsed)
        self.adapter, self.previous = adapter, record
        self.last_time, self.last_request, self.last = now, requested, result
        return dict(result)


class ProcessSampler:
    def __init__(self):
        self.previous = {}

    def sample(self):
        now = time.monotonic()
        rows, current = [], {}
        for proc in psutil.process_iter(['pid', 'name', 'create_time', 'cpu_times', 'memory_info'], ad_value=None):
            try:
                info = proc.info
                if info['pid'] == 0 or info['cpu_times'] is None or info['memory_info'] is None:
                    continue
                key = (info['pid'], info['create_time'])
                elapsed_cpu = info['cpu_times'].user + info['cpu_times'].system
                current[key] = (elapsed_cpu, now)
                old = self.previous.get(key)
                cpu = (max(0, elapsed_cpu - old[0]) / (now - old[1]) * 100 /
                       (psutil.cpu_count() or 1)) if old and now > old[1] else None
                rows.append(dict(name=info['name'] or f"PID {info['pid']}",
                                 cpu=min(100, cpu) if cpu is not None else None,
                                 ram=info['memory_info'].rss))
            except (psutil.Error, OSError):
                continue
        self.previous = current
        return dict(top_cpu=sorted([r for r in rows if r['cpu'] is not None],
                                   key=lambda r: r['cpu'], reverse=True)[:3],
                    top_ram=sorted(rows, key=lambda r: r['ram'], reverse=True)[:3])


def merge_sensor_samples(previous, readings, now, grace=5.0):
    """Bridge brief provider gaps, but expire unavailable values after five seconds."""
    merged = {key: item for key, item in previous.items() if now - item[1] <= grace}
    for key in ('cpu_temp', 'gpu', 'gpu_temp'):
        value = number(readings.get(key))
        if value is not None:
            merged[key] = (value, now)
    # Keep the label paired with the temperature sample that supplied it.
    if number(readings.get('gpu_temp')) is not None:
        merged['gpu_temp_name'] = (readings.get('gpu_temp_name', 'Temperature'), now)
    if number(readings.get('gpu')) is not None:
        merged['gpu_engine_name'] = (readings.get('gpu_engine_name', 'GPU core'), now)
    if readings.get('gpu_name'):
        merged['gpu_name'] = (readings['gpu_name'], now)
    for key in ('hardware', 'cpu_clock', 'gpu_device'):
        if readings.get(key) is not None:
            merged[key] = (readings[key], now)
    result = {key: item[0] for key, item in merged.items()}
    for key in ('cpu_temp', 'gpu_temp', 'gpu', 'hardware'):
        if key in merged:
            result[key + '_age'] = now - merged[key][1]
    return merged, result


class Sensors:
    """Slow vendor calls run on a daemon thread, never on the UI thread."""
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def run(self):
        com = None
        wmi = None
        if os.name == 'nt':
            try:
                import pythoncom
                import wmi
                pythoncom.CoInitialize()
                com = pythoncom
            except ImportError:
                wmi = None
        provider = None
        retry_at = 0
        windows_probe_at = 0
        windows_provider = False
        web_probe_at = 0
        web_client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        recent_samples = {}
        process_sampler = ProcessSampler()
        smi = shutil.which('nvidia-smi')
        if not smi and os.name == 'nt':
            candidate = os.path.join(os.environ.get('ProgramW6432', r'C:\Program Files'),
                                     'NVIDIA Corporation', 'NVSMI', 'nvidia-smi.exe')
            smi = candidate if os.path.isfile(candidate) else None
        try:
            while not self.stop.is_set():
                started = time.monotonic()
                data = {}
                web_readings = {}
                if started >= web_probe_at:
                    try:
                        with web_client.open('http://127.0.0.1:8085/data.json', timeout=0.5) as response:
                            web_readings = parse_lhm_tree(json.load(response))
                        web_probe_at = started + 1
                    except (OSError, ValueError, TypeError):
                        web_probe_at = started + 5
                if wmi and provider is None and started >= retry_at:
                    retry_at = started + 15
                    for namespace in ('root/LibreHardwareMonitor', 'root/OpenHardwareMonitor'):
                        try:
                            provider = wmi.WMI(namespace=namespace)
                            break
                        except Exception:
                            pass
                if provider is not None:
                    try:
                        temps = []
                        gpu = {}
                        sensor_records = []
                        for sensor in provider.Sensor():
                            sensor_records.append(dict(Identifier=str(sensor.Identifier),
                                                       Name=str(sensor.Name), Value=number(sensor.Value)))
                            value = number(sensor.Value)
                            if value is None:
                                continue
                            ident = str(sensor.Identifier).lower()
                            name = str(sensor.Name).lower()
                            kind = str(sensor.SensorType)
                            if kind == 'Temperature' and ('intelcpu' in ident or 'amdcpu' in ident):
                                temps.append(value)
                            if 'gpu' in ident:
                                device = ident.split('/load/')[0].split('/temperature/')[0]
                                record = gpu.setdefault(device, {})
                                if kind == 'Load' and name == 'gpu core':
                                    record['gpu'] = value
                                elif kind == 'Temperature' and ('gpu_temp' not in record or name == 'gpu core'):
                                    record['gpu_temp'] = value
                                    record['gpu_temp_name'] = str(sensor.Name)
                        if temps:
                            data['cpu_temp'] = max(temps)
                        if gpu:
                            data.update(gpu[sorted(gpu)[0]])
                        data.update(parse_sensor_records(sensor_records))
                    except Exception:
                        provider = None
                # Use Windows' own WMI client when optional Python WMI packages
                # are absent. Libre Hardware Monitor must still be running.
                if os.name == 'nt' and wmi is None and not web_readings and started >= windows_probe_at:
                    windows_probe_at = started + (1 if windows_provider else 15)
                    query = r'''
$ErrorActionPreference = 'Stop'
$sensors = $null
foreach ($ns in @('root/LibreHardwareMonitor', 'root/OpenHardwareMonitor')) {
    try { $sensors = @(Get-CimInstance -Namespace $ns -ClassName Sensor); break } catch {}
}
if ($null -eq $sensors) { exit 2 }
$gpu = @($sensors | Where-Object { $_.Identifier -match '/gpu-' -and $_.SensorType -eq 'Temperature' } | Sort-Object Parent, @{Expression={if ($_.Name -eq 'GPU Core') {0} else {1}}}, Name | Select-Object -First 1)
$result = @{}
$result.hardware_records = @($sensors | Select-Object Identifier, Name, SensorType, Value)
if ($gpu.Count -gt 0) {
    $result.gpu_temp = $gpu[0].Value
    $result.gpu_temp_name = $gpu[0].Name
    $load = $sensors | Where-Object { $_.Parent -eq $gpu[0].Parent -and $_.SensorType -eq 'Load' -and $_.Name -eq 'GPU Core' } | Select-Object -First 1
    if ($null -ne $load) { $result.gpu = $load.Value }
}
$cpu = $sensors | Where-Object { $_.Identifier -match '/(intelcpu|amdcpu)/' -and $_.SensorType -eq 'Temperature' } | Measure-Object -Property Value -Maximum
if ($null -ne $cpu.Maximum) { $result.cpu_temp = $cpu.Maximum }
ConvertTo-Json -InputObject $result -Depth 5 -Compress
'''
                    try:
                        powershell = os.path.join(os.environ.get('WINDIR', r'C:\Windows'),
                                                  'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
                        result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-Command', query],
                            capture_output=True, text=True, timeout=3, check=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        readings = json.loads(result.stdout)
                        data.update({key: number(value) for key, value in readings.items()
                                     if key in ('gpu', 'gpu_temp', 'cpu_temp')})
                        if 'gpu_temp_name' in readings:
                            data['gpu_temp_name'] = str(readings['gpu_temp_name'])
                        if 'hardware_records' in readings:
                            data.update(parse_sensor_records(readings['hardware_records']))
                        windows_provider = True
                    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
                        windows_provider = False
                        windows_probe_at = started + 15
                if 'cpu_temp' not in data and hasattr(psutil, 'sensors_temperatures'):
                    try:
                        temps = psutil.sensors_temperatures()
                        values = [s.current for k, readings in temps.items()
                                  if k in ('coretemp', 'k10temp', 'cpu_thermal') for s in readings
                                  if number(s.current) is not None]
                        if values:
                            data['cpu_temp'] = max(values)
                    except Exception:
                        pass
                if smi:
                    try:
                        result = subprocess.run(
                            [smi, '--query-gpu=utilization.gpu,temperature.gpu,name,memory.used,memory.total,clocks.current.graphics,clocks.current.memory',
                             '--format=csv,noheader,nounits', '--id=0'],
                            capture_output=True, text=True, timeout=0.8,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                            check=True)
                        row = next(csv.reader(result.stdout.splitlines()))
                        # Keep utilization and temperature tied to the same adapter.
                        data['gpu'] = number(row[0])
                        data['gpu_temp'] = number(row[1])
                        data['gpu_temp_name'] = 'GPU Core'
                        data['gpu_engine_name'] = 'GPU Core'
                        data['gpu_name'] = row[2].strip()
                        data['nvidia_details'] = dict(zip(('memory_used', 'memory_total', 'core_clock', 'memory_clock'),
                                                         (number(v) for v in row[3:7])))
                    except (OSError, subprocess.SubprocessError, StopIteration, IndexError):
                        pass
                # Prefer the complete GPU pair from the web provider when present.
                if 'gpu_temp' in web_readings or 'gpu' in web_readings:
                    data.pop('gpu', None)
                    data.pop('gpu_temp', None)
                    data.pop('gpu_temp_name', None)
                    data.pop('gpu_engine_name', None)
                data.update(web_readings)
                nvidia_details = data.get('nvidia_details')
                recent_samples, data = merge_sensor_samples(recent_samples, data, time.monotonic())
                if nvidia_details:
                    data['nvidia_details'] = nvidia_details
                try:
                    data.update(process_sampler.sample())
                except (psutil.Error, OSError):
                    data.update(top_cpu=[], top_ram=[])
                with self.lock:
                    self.data = data
                self.stop.wait(max(0, 1 - (time.monotonic() - started)))
        finally:
            if com:
                com.CoUninitialize()


ROW_MIME = 'application/x-pc-monitor-row'


class RowDragHandle(QWidget):
    def __init__(self, monitor, key):
        super().__init__()
        self.monitor, self.key, self.start = monitor, key, None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip('Drag up or down to reorder this item')
        self.setAccessibleName('Drag to reorder')
        self.setFixedWidth(24)
        self.setMinimumHeight(24)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#7ac2ff'))
        for x in (7, 14):
            for y in (-5, 0, 5):
                painter.drawEllipse(x, self.height() // 2 + y - 1, 3, 3)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start = event.position().toPoint()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.start is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if (event.position().toPoint() - self.start).manhattanLength() >= QApplication.startDragDistance():
                self.start = None
                drag = QDrag(self)
                mime = QMimeData()
                mime.setData(ROW_MIME, self.key.encode('ascii'))
                drag.setMimeData(mime)
                drag.setPixmap(self.monitor.panel_widgets()[self.key].grab())
                drag.exec(Qt.DropAction.MoveAction)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.start = None
        event.accept()


class ReorderList(QWidget):
    def __init__(self, monitor):
        super().__init__()
        self.monitor = monitor
        self.drop_y = None
        self.scroll_direction = 0
        self.auto_scroll = QTimer(self)
        self.auto_scroll.setInterval(35)
        self.auto_scroll.timeout.connect(self.scroll_during_drag)
        self.setAcceptDrops(True)

    def scroll_during_drag(self):
        bar = self.monitor.scroll.verticalScrollBar()
        old = bar.value()
        bar.setValue(old + self.scroll_direction * 12)
        if self.drop_y is not None:
            self.drop_y += bar.value() - old
            self.update()

    def accepts(self, event):
        return event.mimeData().hasFormat(ROW_MIME) and isinstance(event.source(), RowDragHandle) and event.source().monitor is self.monitor

    def dragEnterEvent(self, event):
        if self.accepts(event):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()

    def dragMoveEvent(self, event):
        if self.accepts(event):
            self.drop_y = event.position().y()
            viewport = self.monitor.scroll.viewport()
            y = self.mapTo(viewport, event.position().toPoint()).y()
            self.scroll_direction = -1 if y < 35 else 1 if y > viewport.height() - 35 else 0
            if self.scroll_direction:
                self.auto_scroll.start()
            else:
                self.auto_scroll.stop()
            self.update()
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()

    def dragLeaveEvent(self, event):
        self.auto_scroll.stop()
        self.drop_y = None
        self.update()
        event.accept()

    def dropEvent(self, event):
        self.auto_scroll.stop()
        if self.accepts(event):
            key = bytes(event.mimeData().data(ROW_MIME)).decode('ascii')
            before = None
            widgets = self.monitor.panel_widgets()
            for item in self.monitor.options['monitor_order']:
                widget = widgets[item]
                if item != key and not widget.isHidden() and event.position().y() < widget.geometry().center().y():
                    before = item
                    break
            self.monitor.move_monitor(key, before)
            self.drop_y = None
            self.update()
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.drop_y is not None:
            painter = QPainter(self)
            painter.setPen(QPen(QColor('#7ac2ff'), 2))
            painter.drawLine(0, round(self.drop_y), self.width(), round(self.drop_y))


class Meter(QWidget):
    def __init__(self, title):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 6)
        header = QHBoxLayout()
        self.header_layout = header
        header.addWidget(QLabel(title))
        self.value = QLabel('N/A')
        header.addStretch()
        header.addWidget(self.value)
        layout.addLayout(header)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        layout.addWidget(self.bar)
        self.detail = QLabel('')
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet('color: #a2aec3; font-size: 11px;')
        layout.addWidget(self.detail)
        self.update_value(None)

    def update_value(self, value, detail=''):
        value = number(value)
        shade = '#657086' if value is None else color(value)
        self.value.setText('N/A' if value is None else f'{value:.1f}%')
        self.value.setStyleSheet(f'color: {shade}; font-size: 19px; font-weight: 600;')
        self.bar.setValue(0 if value is None else round(max(0, min(100, value)) * 10))
        self.bar.setStyleSheet('QProgressBar {background: #293245; border: none; border-radius: 4px;}'
                              f'QProgressBar::chunk {{background: {shade}; border-radius: 4px;}}')
        self.detail.setText(detail)


class DetailSection(QWidget):
    def __init__(self, title, expanded, changed):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        self.toggle = QToolButton()
        self.toggle.setText(title.replace('&', '&&'))
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setStyleSheet('QToolButton {color: #b9d9f5; background: #202c40; border: none;'
                                 'border-radius: 6px; padding: 9px; font-size: 12px; text-align: left;}')
        self.toggle.setSizePolicy(self.sizePolicy().horizontalPolicy(), self.sizePolicy().verticalPolicy())
        self.header_layout = QHBoxLayout()
        self.header_layout.addWidget(self.toggle, 1)
        layout.addLayout(self.header_layout)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(4, 6, 4, 4)
        self.label = QLabel('Waiting for first sample…')
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setStyleSheet('color: #b5c2d6; font-size: 12px;')
        self.body_layout.addWidget(self.label)
        layout.addWidget(self.body)
        self.set_expanded(expanded)
        self.toggle.toggled.connect(lambda value: (self.set_expanded(value), changed(value)))

    def set_expanded(self, value):
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if value else Qt.ArrowType.RightArrow)
        self.body.setVisible(value)


class ResizeGrip(QWidget):
    """Visible corner handle for resizing the frameless window."""
    def __init__(self, monitor):
        super().__init__(monitor)
        self.monitor = monitor
        self.origin = None
        self.setFixedSize(24, 24)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setToolTip('Drag to resize')

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#7ac2ff'), 2, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        for length in (4, 9, 14):
            painter.drawLine(19 - length, 19, 19, 19 - length)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.globalPosition().toPoint()
            self.start_size = self.monitor.size()
            self.grabMouse()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.origin is not None:
            delta = event.globalPosition().toPoint() - self.origin
            minimum = self.monitor.minimumSizeHint()
            self.monitor.resize(
                max(320, minimum.width(), min(1200, self.start_size.width() + delta.x())),
                max(minimum.height(), min(1600, self.start_size.height() + delta.y())))
            event.accept()

    def mouseReleaseEvent(self, event):
        if self.origin is not None:
            self.origin = None
            self.releaseMouse()
            self.monitor.save_settings()
            event.accept()


class PanelHeader(QWidget):
    def __init__(self, panel, title):
        super().__init__(panel)
        self.panel = panel
        self.drag = None
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(title)
        label.setStyleSheet('font-size: 11px; color: #91bde4; font-weight: 600;')
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(label, 1)
        close = QPushButton('×')
        close.setFixedSize(22, 22)
        close.setToolTip('Hide this panel; restore it in Settings → Monitoring')
        close.clicked.connect(panel.close)
        layout.addWidget(close)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.panel.options['locked']:
            self.drag = event.globalPosition().toPoint() - self.panel.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.panel.move(event.globalPosition().toPoint() - self.drag)
            event.accept()

    def mouseReleaseEvent(self, event):
        if self.drag is not None:
            self.drag = None
            self.panel.save_settings()
            event.accept()


class FloatingPanel(QWidget):
    def __init__(self, owner, key, title, content):
        super().__init__(owner, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.owner, self.key = owner, key
        self.setWindowTitle('PC Monitor · ' + title)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(280, 140)
        self.setMaximumSize(1200, 1600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 27)
        self.header = PanelHeader(self, title)
        layout.addWidget(self.header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(owner.scroll.styleSheet())
        self.scroll.setWidget(content)
        layout.addWidget(self.scroll, 1)
        self.grip = ResizeGrip(self)

    @property
    def options(self):
        return self.owner.options

    def save_settings(self):
        self.owner.options['panel_positions'][self.key] = dict(x=self.x(), y=self.y(),
                                                              width=self.width(), height=self.height())
        self.owner.save_settings()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(115, 139, 176, 130), 1))
        painter.setBrush(QColor(16, 22, 34, round(self.options['opacity'] * 2.55)))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 16, 16)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'grip'):
            self.grip.move(self.width() - 28, self.height() - 28)
            self.grip.raise_()

    def closeEvent(self, event):
        self.save_settings()
        if not self.owner.closing:
            self.owner.options['show_' + self.key] = False
            self.owner.save_settings()
        event.accept()


class Monitor(QWidget):
    def __init__(self):
        super().__init__()
        self.options = load_settings()
        self.closing = False
        self.panels = {}
        self.floating_active = False
        self.setWindowTitle('PC Monitor')
        self.setWindowIcon(QIcon(str(ROOT / 'pc_monitor.ico')))
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumWidth(320)
        self.setMaximumSize(1200, 1600)
        self.drag = None
        self.setStyleSheet('QLabel {color: #e6edf8; font-family: "Segoe UI"; font-size: 13px;}'
                          'QPushButton {color: #b8c4d8; background: transparent; border: none;'
                          'font-size: 20px;} QPushButton:hover {color: #ff6577;}')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 16, 22, 28)
        title = QHBoxLayout()
        label = QLabel('PC MONITOR')
        label.setStyleSheet('font-weight: 700; font-size: 16px;')
        title.addWidget(label)
        title.addStretch()
        settings = QPushButton('Settings')
        settings.setStyleSheet('font-size: 12px; padding: 5px; color: #7ac2ff;')
        settings.setToolTip('Appearance and monitoring settings')
        settings.clicked.connect(self.open_settings)
        title.addWidget(settings)
        close = QPushButton('×')
        close.setFixedSize(26, 26)
        close.setToolTip('Close monitor')
        close.clicked.connect(self.close)
        title.addWidget(close)
        layout.addLayout(title)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet('QScrollArea, QScrollArea > QWidget > QWidget {background: transparent;}'
                                 'QScrollBar:vertical {background: #172233; width: 7px; border-radius: 3px;}'
                                 'QScrollBar::handle:vertical {background: #4d6a8a; min-height: 25px; border-radius: 3px;}'
                                 'QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height: 0px;}')
        content = ReorderList(self)
        content_layout = QVBoxLayout(content)
        self.content_layout = content_layout
        content_layout.setContentsMargins(0, 0, 8, 0)
        self.scroll.setWidget(content)
        layout.addWidget(self.scroll, 1)
        self.meters = {}
        for key, text in [('cpu', 'CPU'), ('ram', 'RAM'), ('disk', 'DISK SPACE'), ('gpu', 'GPU')]:
            meter = Meter(text)
            self.meters[key] = meter
            content_layout.addWidget(meter)
        self.sections = {}
        for key, title, option in [('network', 'NETWORK SPEED', 'network'),
                                    ('activity', 'DISK ACTIVITY', 'activity'),
                                    ('cores', 'CPU CLOCK & LOGICAL CORES', 'cpu'),
                                    ('graphics', 'GPU MEMORY & CLOCKS', 'gpu'),
                                    ('battery', 'BATTERY', 'battery'),
                                    ('hardware', 'FANS & TEMPERATURES', 'hardware'),
                                    ('apps', 'TOP APPS', 'apps')]:
            section = DetailSection(title, self.options['expand_' + option],
                                    lambda value, name=option: self.save_expanded(name, value))
            self.sections[key] = section
            content_layout.addWidget(section)
        self.core_meters = []
        self.core_grid = QFormLayout()
        self.sections['cores'].body_layout.addLayout(self.core_grid)
        content_layout.addStretch()
        self.row_handles = {}
        for key, widget in self.panel_widgets().items():
            handle = RowDragHandle(self, key)
            widget.header_layout.insertWidget(0, handle)
            self.row_handles[key] = handle
        self.panel_controls = QWidget()
        controls_layout = QVBoxLayout(self.panel_controls)
        controls_layout.setContentsMargins(0, 4, 0, 4)
        help_text = QLabel('Drag each panel by its title.\nChoose panels in Settings → Monitoring.')
        help_text.setStyleSheet('color: #a2aec3; font-size: 12px;')
        controls_layout.addWidget(help_text)
        buttons = QHBoxLayout()
        arrange = QPushButton('Arrange panels')
        together = QPushButton('Keep together')
        for button in (arrange, together):
            button.setStyleSheet('font-size: 12px; color: #7ac2ff; padding: 6px; background: #202c40; border-radius: 5px;')
            buttons.addWidget(button)
        arrange.clicked.connect(self.arrange_panels)
        together.clicked.connect(self.dock_panels)
        controls_layout.addLayout(buttons)
        layout.addWidget(self.panel_controls)
        self.previous_counters = None
        self.network_sampler = NetworkSampler()
        self.counter_time = time.monotonic()
        self.status = QLabel('Starting sensors…  •  Drag to move')
        self.status.setStyleSheet('color: #8290a8; font-size: 11px;')
        layout.addWidget(self.status)
        self.grip = ResizeGrip(self)
        self.grip.raise_()
        self.sensors = Sensors()
        self.drive = self.options['drive'] or (os.path.abspath(os.environ.get('SystemDrive', 'C:') + os.sep) if os.name == 'nt' else '/')
        psutil.cpu_percent(None)  # Prime the nonblocking CPU sample.
        psutil.cpu_percent(None, percpu=True)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.apply_settings()
        self.refresh(first=True)
        if self.options['x'] is not None and self.options['y'] is not None:
            self.move(self.options['x'], self.options['y'])
            if not any(screen.availableGeometry().intersects(self.frameGeometry())
                       for screen in QApplication.screens()):
                self.move(QApplication.primaryScreen().availableGeometry().topLeft())

    def save_settings(self):
        self.options.update(x=self.x(), y=self.y())
        if not self.options['separate_panels']:
            self.options.update(width=self.width(), height=self.height())
        try:
            temporary = CONFIG.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.options, indent=2), encoding='utf-8')
            temporary.replace(CONFIG)
        except OSError:
            QMessageBox.warning(self, 'Settings', 'Could not save settings in the widget folder.')

    def save_expanded(self, name, value):
        self.options['expand_' + name] = value
        self.save_settings()

    def panel_widgets(self):
        return {**self.meters, **self.sections}

    def apply_order(self):
        widgets = self.panel_widgets()
        for index, key in enumerate(self.options['monitor_order']):
            self.content_layout.removeWidget(widgets[key])
            self.content_layout.insertWidget(index, widgets[key])

    def move_monitor(self, key, before=None):
        order = self.options['monitor_order']
        if key not in order or before == key:
            return
        order.remove(key)
        order.insert(order.index(before) if before in order else len(order), key)
        self.apply_order()
        self.save_settings()

    def position_panel(self, panel, index, restore=True):
        area = QApplication.primaryScreen().availableGeometry()
        columns = max(1, area.width() // 320)
        rows = max(1, area.height() // 240)
        width, height = 300, min(220, area.height())
        x = area.x() + 10 + (index % columns) * 320
        y = area.y() + 10 + ((index // columns) % rows) * 240
        saved = self.options['panel_positions'].get(panel.key, {}) if restore else {}
        if isinstance(saved, dict) and all(type(saved.get(k)) is int for k in ('x', 'y', 'width', 'height')):
            x, y = saved['x'], saved['y']
            width, height = max(280, min(1200, saved['width'])), max(140, min(1600, saved['height']))
        panel.resize(width, height)
        panel.move(x, y)
        if not any(screen.availableGeometry().intersects(panel.frameGeometry()) for screen in QApplication.screens()):
            panel.move(area.topLeft())

    def arrange_panels(self):
        self.options['panel_positions'] = {}
        for index, panel in enumerate(p for p in self.panels.values() if self.options['show_' + p.key]):
            self.position_panel(panel, index, restore=False)
            panel.save_settings()

    def dock_panels(self):
        self.options['separate_panels'] = False
        self.apply_settings()
        self.save_settings()

    def sync_panels(self):
        separate = self.options['separate_panels']
        if separate != self.floating_active:
            if separate:
                titles = dict(MONITORS)
                for index, (key, widget) in enumerate(self.panel_widgets().items()):
                    if isinstance(widget, DetailSection):
                        widget.toggle.hide()
                        widget.body.show()
                    panel = FloatingPanel(self, key, titles[key], widget)
                    self.panels[key] = panel
                    self.position_panel(panel, index)
            else:
                for index, (key, widget) in enumerate(self.panel_widgets().items()):
                    panel = self.panels.pop(key)
                    panel.save_settings()
                    panel.scroll.takeWidget()
                    widget.setParent(self.scroll.widget())
                    self.content_layout.insertWidget(index, widget)
                    if isinstance(widget, DetailSection):
                        widget.toggle.show()
                        widget.set_expanded(widget.toggle.isChecked())
                    panel.hide()
                    panel.deleteLater()
            self.floating_active = separate
        self.scroll.setVisible(not separate)
        self.panel_controls.setVisible(separate)
        for key, panel in self.panels.items():
            panel.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.options['top'])
            panel.update()
            panel.setVisible(self.options['show_' + key])

    def open_settings(self):
        dialog = SettingsDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.values()
            if values['start_windows'] != self.options['start_windows']:
                try:
                    set_windows_startup(values['start_windows'])
                except OSError as error:
                    QMessageBox.warning(self, 'Start with Windows', str(error))
                    values['start_windows'] = self.options['start_windows']
            self.options.update(values)
            self.apply_settings()
            self.save_settings()
            self.refresh()

    def apply_settings(self):
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.options['top'])
        self.timer.setInterval(self.options['refresh'] * 1000)
        self.drive = self.options['drive'] or self.drive
        self.sync_panels()
        self.apply_order()
        for key, meter in self.meters.items():
            meter.setVisible(self.options['show_' + key])
            meter.detail.setVisible(not self.options['compact'])
        for key, section in self.sections.items():
            section.setVisible(self.options['show_' + key])
        self.layout().activate()
        minimum = self.minimumSizeHint()
        self.resize(max(self.options['width'], minimum.width()),
                    max(190 if self.options['separate_panels'] else (self.options['height'] or 600),
                        minimum.height()))
        self.update()
        if visible:
            self.show()

    def reveal(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        for key, panel in self.panels.items():
            if self.options['show_' + key]:
                panel.showNormal()
                panel.raise_()

    def refresh(self, first=False):
        data = self.sensors.snapshot()
        self.meters['cpu'].update_value(None if first else psutil.cpu_percent(None),
                                        ('Temperature: ' + fahrenheit(data.get('cpu_temp'))
                                         + (' (last reading)' if data.get('cpu_temp_age', 0) > 2 else ''))
                                        if self.options['temperatures'] else 'Live processor usage')
        mem = psutil.virtual_memory()
        self.meters['ram'].update_value(mem.percent, f'{mem.used / 2**30:.1f} / {mem.total / 2**30:.1f} GB used')
        try:
            disk = psutil.disk_usage(self.drive)
            self.meters['disk'].update_value(disk.percent,
                f'{self.drive}  ·  {disk.used / 2**30:.1f} / {disk.total / 2**30:.1f} GB used')
        except OSError:
            self.meters['disk'].update_value(None, 'Drive unavailable')
        self.meters['gpu'].update_value(data.get('gpu'),
                                        (data.get('gpu_temp_name', 'Temperature') + ': ' + fahrenheit(data.get('gpu_temp'))
                                         + (' (last reading)' if data.get('gpu_temp_age', 0) > 2 else '')
                                         if data.get('gpu_temp') is not None else 'Temperature N/A · sensor provider needed')
                                        if self.options['temperatures'] else 'Graphics processor usage')
        self.meters['gpu'].setToolTip(data.get('gpu_name', 'First detected GPU') +
                                     '\nUsage source: ' + data.get('gpu_engine_name', 'N/A') +
                                     '\nBar = busiest exposed GPU engine, not temperature.\n'
                                     'Brief sensor gaps retain the last reading for up to 5 seconds.')
        state = 'Position locked' if self.options['locked'] else 'Drag to move'
        self.status.setText(f"LIVE  ·  {self.options['refresh']}s refresh  ·  {state}")
        self.refresh_details(data, first)

    def refresh_details(self, data, first=False):
        now = time.monotonic()
        counters = {}
        network = self.network_sampler.sample(self.options['network_adapter'])
        try:
            disk = psutil.disk_io_counters()
            if disk:
                counters.update(read=disk.read_bytes, write=disk.write_bytes)
        except (OSError, psutil.Error):
            pass
        elapsed = now - self.counter_time
        old = self.previous_counters or {}
        rates = {key: byte_rate(counter_rate(counters.get(key), old.get(key), elapsed))
                 for key in ('read', 'write')}
        self.previous_counters, self.counter_time = counters, now
        self.sections['network'].label.setText(
            f"Download   {network_rate(network['download'], self.options['network_unit'])}\n"
            f"Upload        {network_rate(network['upload'], self.options['network_unit'])}\n"
            + ('Current traffic · ' + network['adapter'] if network['adapter'] else 'No active adapter · select one in Settings'))
        self.sections['network'].label.setToolTip('Actual adapter traffic during the last refresh interval.\n'
                                                 'This is not your connection’s maximum speed.\n'
                                                 'Mbps = megabits/s; MB/s = megabytes/s. 8 Mbps = 1 MB/s.')
        self.sections['activity'].label.setText(
            f"Disk read    {rates['read']}\nDisk write    {rates['write']}\n"
            'Combined activity across all disks')

        try:
            cores = psutil.cpu_percent(None, percpu=True)
        except (OSError, psutil.Error):
            cores = []
        while len(self.core_meters) < len(cores):
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setFixedHeight(18)
            self.core_grid.addRow(f'Logical CPU {len(self.core_meters) + 1}', bar)
            self.core_meters.append(bar)
        for index, bar in enumerate(self.core_meters):
            self.core_grid.setRowVisible(index, index < len(cores))
            if index < len(cores):
                value = cores[index]
                bar.setValue(round(value * 10))
                bar.setFormat('Sampling…' if first else f'{value:.1f}%')
                bar.setStyleSheet('QProgressBar {background: #273449; border: none; border-radius: 4px;'
                                 'color: white; font-size: 10px; text-align: center;}'
                                 f'QProgressBar::chunk {{background: {color(value)}; border-radius: 4px;}}')
        mhz, clock_label = data.get('cpu_clock'), 'Live core average'
        if mhz is None:
            try:
                frequency = psutil.cpu_freq()
                mhz = frequency.current if frequency else None
                clock_label = 'Reported CPU frequency'
            except (OSError, psutil.Error, NotImplementedError):
                pass
        self.sections['cores'].label.setText(
            f'{clock_label}: {mhz / 1000:.2f} GHz' if mhz else 'CPU clock: N/A')

        hardware = data.get('hardware', [])
        selected_gpu = data.get('gpu_device')
        gpu_lines = []
        for sensor in hardware:
            if '/gpu-' not in sensor['id'] or (selected_gpu and not sensor['id'].startswith(selected_gpu + '/')):
                continue
            kind = sensor['kind']
            if kind in ('clock', 'data', 'smalldata'):
                unit = {'clock': 'MHz', 'data': 'GB', 'smalldata': 'MB'}[kind]
                gpu_lines.append(f"{sensor['name']}: {sensor['value']:.1f} {unit}")
            elif kind == 'load' and (sensor['name'].lower() == 'gpu core' or sensor['name'].lower().startswith('d3d')):
                gpu_lines.append(f"{sensor['name']}: {sensor['value']:.1f}%")
        nvidia = data.get('nvidia_details', {})
        for key, label, unit in [('memory_used', 'Memory used', 'MiB'),
                                  ('memory_total', 'Memory total', 'MiB'),
                                  ('core_clock', 'Graphics clock', 'MHz'),
                                  ('memory_clock', 'Memory clock', 'MHz')]:
            if nvidia.get(key) is not None:
                gpu_lines.append(f'{label}: {nvidia[key]:.1f} {unit}')
        self.sections['graphics'].label.setText('\n'.join(gpu_lines) or 'GPU memory and clocks: N/A\nWaiting for supported hardware sensors.')

        try:
            battery = psutil.sensors_battery()
        except (AttributeError, OSError, psutil.Error, NotImplementedError):
            battery = None
        if battery is None:
            battery_text = 'No battery detected'
        else:
            state = ('Plugged in · fully charged' if battery.percent >= 99.9 else 'Plugged in · charging') if battery.power_plugged else 'Running on battery'
            battery_text = f'{battery.percent:.1f}%  ·  {state}'
            if not battery.power_plugged and battery.secsleft > 0:
                minutes = int(battery.secsleft) // 60
                battery_text += f'\nEstimated remaining: {minutes // 60}h {minutes % 60}m'
        self.sections['battery'].label.setText(battery_text)

        hardware_lines = []
        if data.get('hardware_age', 0) > 2:
            hardware_lines.append('Last sensor reading (brief connection gap)')
        for sensor in sorted(hardware, key=lambda s: (s['device'], s['kind'], s['name'])):
            if sensor['kind'] == 'fan':
                value = f"{sensor['value']:.0f} RPM"
            elif sensor['kind'] == 'temperature' and self.options['temperatures']:
                value = fahrenheit(sensor['value'])
            else:
                continue
            hardware_lines.append(f"{sensor['device']} · {sensor['name']}: {value}")
        if not hardware_lines:
            hardware_lines = ['Fans and temperature sensors: N/A\nLibre Hardware Monitor supplies supported sensors.']
        self.sections['hardware'].label.setText('\n'.join(hardware_lines))

        app_lines = ['MOST CPU · share of total processor capacity']
        cpu_rows = data.get('top_cpu', [])
        if cpu_rows:
            app_lines.extend(f"{row['name']}   {row['cpu']:.1f}%" for row in cpu_rows)
        else:
            app_lines.append('Sampling…' if not data else 'N/A · waiting for process samples')
        app_lines.extend(['', 'MOST RAM · working set'])
        ram_rows = data.get('top_ram', [])
        if ram_rows:
            app_lines.extend(f"{row['name']}   {row['ram'] / 2**20:.1f} MiB" for row in ram_rows)
        else:
            app_lines.append('N/A · waiting for process samples')
        self.sections['apps'].label.setText('\n'.join(app_lines))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(16, 22, 34, round(self.options['opacity'] * 2.55)))
        painter.setPen(QPen(QColor(115, 139, 176, 100), 1))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 20, 20)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'grip'):
            self.grip.move(self.width() - 29, self.height() - 29)
            self.grip.raise_()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.options['locked']:
            self.drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag)
            event.accept()

    def mouseReleaseEvent(self, event):
        if self.drag is not None:
            self.save_settings()
        self.drag = None

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self.closing = True
        for panel in self.panels.values():
            panel.close()
        self.timer.stop()
        self.sensors.stop.set()
        self.save_settings()
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    # The lock prevents simultaneous launches; IPC brings the existing window forward.
    lock = QLockFile(str(ROOT / 'pc_monitor.lock'))
    lock.setStaleLockTime(0)
    server_name = 'CodexPCMonitor_' + __import__('hashlib').sha256(str(ROOT).lower().encode()).hexdigest()[:20]
    if not lock.tryLock(0):
        client = QLocalSocket()
        client.connectToServer(server_name)
        if client.waitForConnected(1500):
            client.disconnectFromServer()
        sys.exit(0)
    server = QLocalServer()
    QLocalServer.removeServer(server_name)
    server.listen(server_name)
    widget = Monitor()
    def activate_existing():
        while server.hasPendingConnections():
            connection = server.nextPendingConnection()
            connection.disconnectFromServer()
            connection.deleteLater()
        widget.reveal()
    server.newConnection.connect(activate_existing)
    widget.show()
    result = app.exec()
    server.close()
    lock.unlock()
    sys.exit(result)
