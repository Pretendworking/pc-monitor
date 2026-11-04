"""Standalone desktop monitor. Install: pip install PySide6 psutil

Optional Windows sensors: pip install WMI pywin32, then run Libre Hardware
Monitor with WMI enabled. NVIDIA GPUs also use nvidia-smi when available.
Disk percentage means system-drive space used, not disk activity.
"""
import csv
from collections import deque
from datetime import datetime, timezone
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
import pc_extras as extras
from PySide6.QtCore import Qt, QTimer, QLockFile, QMimeData, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QIcon, QDrag, QPainterPath, QLinearGradient
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget, QDialog,
                               QFormLayout, QSpinBox, QCheckBox, QComboBox,
                               QDialogButtonBox, QMessageBox, QScrollArea,
                               QToolButton, QFrame, QTabWidget, QToolTip, QGridLayout, QSizePolicy, QLayout, QFileDialog)

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
DEFAULTS.update(history_cpu=False, history_ram=False, history_disk=False, history_gpu=False)
DEFAULTS.update(dashboard=True, dashboard_width=1100, dashboard_height=780)
DEFAULTS.update(accent='Cyan', glow=45, text_size=13, gauges=True, card_heights={},
                card_spans={}, dashboard_order=['cpu', 'gpu', 'ram', 'disk'], favorite_apps=[], presentmon_path='')
DEFAULTS.update(floating_enabled=False, alerts_enabled=False, alert_sound=False,
                alert_cpu=90, alert_ram=90, alert_cpu_temp=185, alert_gpu_temp=185, alert_seconds=15)
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
    result['card_heights'] = {}
    result['card_spans'] = {}
    result['favorite_apps'] = []
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
    result['separate_panels'] = result['floating_enabled']
    for key, low, high in [('alert_cpu', 1, 100), ('alert_ram', 1, 100),
                           ('alert_cpu_temp', 80, 230), ('alert_gpu_temp', 80, 230), ('alert_seconds', 5, 300)]:
        result[key] = max(low, min(high, result[key]))
    order = result.get('monitor_order', [])
    valid = [key for key, _ in MONITORS]
    result['monitor_order'] = list(dict.fromkeys(key for key in order if isinstance(key, str) and key in valid))
    result['monitor_order'].extend(key for key in valid if key not in result['monitor_order'])
    result['dashboard_order'] = list(dict.fromkeys(key for key in result['dashboard_order']
                                                if isinstance(key, str) and key in ('cpu', 'gpu', 'ram', 'disk')))
    result['dashboard_order'].extend(key for key in ('cpu', 'gpu', 'ram', 'disk') if key not in result['dashboard_order'])
    result['favorite_apps'] = [path for path in result['favorite_apps'] if isinstance(path, str) and Path(path).suffix.lower() == '.exe'][:6]
    result['card_heights'] = {key: max(90, min(300, height)) for key, height in result['card_heights'].items()
                             if key in ('cpu', 'gpu', 'ram', 'disk') and type(height) is int}
    result['card_spans'] = {key: span for key, span in result['card_spans'].items()
                           if key in ('cpu', 'gpu', 'ram', 'disk') and type(span) is int and span in (1, 2)}
    result['glow'] = max(0, min(100, result['glow']))
    result['text_size'] = max(10, min(16, result['text_size']))
    if result['accent'] not in extras.ACCENTS:
        result['accent'] = 'Cyan'
    return result


class SettingsDialog(QDialog):
    def __init__(self, monitor):
        super().__init__(monitor)
        self.setWindowTitle('PC Monitor settings')
        self.setMinimumWidth(360)
        self.setStyleSheet('QDialog {background: #151e2c; color: #d9f6ff;}'
                          'QLabel,QCheckBox {color: #d9f6ff; font-size: 13px;}'
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
        theme = QWidget()
        theme_form = QFormLayout(theme)
        self.accent = QComboBox()
        self.accent.addItems(list(extras.ACCENTS))
        self.accent.setCurrentText(monitor.options['accent'])
        theme_form.addRow('Theme / accent', self.accent)
        for key, title, low, high in [('glow', 'Edge glow', 0, 100), ('text_size', 'Text size', 10, 16)]:
            control = QSpinBox()
            control.setRange(low, high)
            control.setValue(monitor.options[key])
            self.controls[key] = control
            theme_form.addRow(title, control)
        gauges = QCheckBox('Circular CPU / GPU gauges')
        gauges.setChecked(monitor.options['gauges'])
        self.controls['gauges'] = gauges
        theme_form.addRow(gauges)
        tabs.addTab(theme, 'Theme')
        alerts = QWidget()
        alerts_form = QFormLayout(alerts)
        for key, title in [('alerts_enabled', 'Enable custom alerts'), ('alert_sound', 'Play a sound for new alerts')]:
            control = QCheckBox(title)
            control.setChecked(monitor.options[key])
            self.controls[key] = control
            alerts_form.addRow(control)
        for key, title, low, high, suffix in [
                ('alert_cpu', 'CPU usage', 1, 100, '%'), ('alert_ram', 'Memory usage', 1, 100, '%'),
                ('alert_cpu_temp', 'CPU temperature', 80, 230, ' °F'),
                ('alert_gpu_temp', 'GPU temperature', 80, 230, ' °F'),
                ('alert_seconds', 'Must stay high for', 5, 300, ' sec')]:
            control = QSpinBox()
            control.setRange(low, high)
            control.setSuffix(suffix)
            control.setValue(monitor.options[key])
            self.controls[key] = control
            alerts_form.addRow(title, control)
        alert_help = QLabel('Alerts appear in the dashboard. Missing sensors do not trigger alerts.\nRepeat alerts are limited to once per metric every 5 minutes.\n\nRecent alerts this session:\n' + ('\n'.join(list(monitor.alerts.history)[:6]) or 'None yet.'))
        alert_help.setWordWrap(True)
        alert_help.setTextFormat(Qt.TextFormat.PlainText)
        alerts_form.addRow(alert_help)
        tabs.addTab(alerts, 'Alerts')
        layout.addWidget(tabs)
        note = QLabel('Colors: blue below 50% · yellow 50–79.9% · red 80%+\n'
                      'Unavailable hardware sensors display N/A.')
        note.setWordWrap(True)
        note.setStyleSheet('color: #9cabbd; font-size: 11px;')
        layout.addWidget(note)
        sensor_help = QLabel('<a style="color:#51e0ef" href="https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases">Get Libre Hardware Monitor for GPU temperatures</a>')
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
        result['accent'] = self.accent.currentText()
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
        painter.setBrush(QColor('#51e0ef'))
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
            painter.setPen(QPen(QColor('#51e0ef'), 2))
            painter.drawLine(0, round(self.drop_y), self.width(), round(self.drop_y))


class DashboardGridArea(ReorderList):
    def scroll_during_drag(self):
        bar = self.monitor.dashboard_scroll.verticalScrollBar()
        bar.setValue(bar.value() + self.scroll_direction * 12)

    def dragMoveEvent(self, event):
        if self.accepts(event):
            viewport = self.monitor.dashboard_scroll.viewport()
            y = self.mapTo(viewport, event.position().toPoint()).y()
            self.scroll_direction = -1 if y < 35 else 1 if y > viewport.height() - 35 else 0
            self.auto_scroll.start() if self.scroll_direction else self.auto_scroll.stop()
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()

    def dropEvent(self, event):
        self.auto_scroll.stop()
        if self.accepts(event):
            key = bytes(event.mimeData().data(ROW_MIME)).decode('ascii')
            candidates = [(name, meter) for name, meter in self.monitor.meters.items()
                          if name != key and not meter.isHidden()]
            if candidates:
                point = event.position().toPoint()
                target = min(candidates, key=lambda pair: (pair[1].geometry().center() - point).manhattanLength())[0]
                self.monitor.move_monitor(key, target)
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()


class HistoryGraph(QWidget):
    """Bounded, timestamped local history. Missing samples break the line."""
    WINDOW = 300

    def __init__(self, series=('Usage',), unit='%', ceiling=100):
        super().__init__()
        self.series, self.unit, self.ceiling = series, unit, ceiling
        self.samples = deque(maxlen=7202)
        self.window = self.WINDOW
        self.hover_x = None
        self.clock_offset = time.time() - time.monotonic()
        self.setMouseTracking(True)
        self.range_selector = QComboBox(self)
        for label, seconds in [('5 min', 300), ('30 min', 1800), ('1 hour', 3600)]:
            self.range_selector.addItem(label, seconds)
        self.range_selector.setGeometry(8, 3, 82, 23)
        self.range_selector.setAccessibleName('History time range')
        self.range_selector.setStyleSheet('QComboBox {color: #7be8fa; background: #0a2430; border: 0; border-radius: 4px; padding: 2px 5px; font-size: 11px;} QComboBox QAbstractItemView {color: #d9f6ff; background: #0a2430;}')
        self.range_selector.currentIndexChanged.connect(self.change_range)
        self.setMinimumHeight(155)
        self.setToolTip('Hover to inspect exact samples. History is collected while PC Monitor runs; gaps mean unavailable readings.')
        self.setAccessibleName('Live ' + ' and '.join(series) + ' history')

    def change_range(self, index):
        self.window = self.range_selector.itemData(index)
        self.update()

    def sample_at(self, timestamp):
        if not self.samples:
            return None
        closest = min(self.samples, key=lambda sample: abs(sample[0] - timestamp))
        return closest if abs(closest[0] - timestamp) <= 15 else None

    def mouseMoveEvent(self, event):
        self.hover_x = max(12, min(self.width() - 12, event.position().x()))
        target = time.monotonic() - (self.width() - 12 - self.hover_x) / max(1, self.width() - 24) * self.window
        sample = self.sample_at(target)
        if sample is None:
            text = 'No sample here — history starts when the app opens.'
        else:
            stamp, values = sample
            clock = time.strftime('%H:%M:%S', time.localtime(stamp + self.clock_offset))
            readings = '\n'.join(f'{name}: ' + ('N/A' if value is None else f'{value:.2f} {self.unit}')
                                 for name, value in zip(self.series, values))
            text = clock + '\n' + readings
        QToolTip.showText(event.globalPosition().toPoint(), text, self)
        self.update()
        event.accept()

    def leaveEvent(self, event):
        self.hover_x = None
        QToolTip.hideText()
        self.update()

    def add_sample(self, values, timestamp=None):
        now = time.monotonic() if timestamp is None else timestamp
        cleaned = tuple(number(value) for value in values)
        if len(cleaned) != len(self.series):
            raise ValueError('One value is required for each series')
        self.samples.append((now, cleaned))
        while self.samples and self.samples[0][0] < now - 3600:
            self.samples.popleft()
        self.update()

    def segments(self, index, now):
        """Do not bridge sensor gaps, sleep, or missed refresh intervals."""
        segments, segment, previous = [], [], None
        for stamp, values in self.samples:
            if stamp < now - self.window:
                continue
            value = values[index]
            if value is None or (previous is not None and stamp - previous > 15):
                if segment:
                    segments.append(segment)
                segment = []
            if value is not None:
                segment.append((stamp, value))
            previous = stamp
        if segment:
            segments.append(segment)
        return segments

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(getattr(self, 'theme_background', '#07151e')))
        painter.drawRoundedRect(QRectF(self.rect()), 8, 8)
        plot = QRectF(12, 26, max(20, self.width() - 24), self.height() - 65)
        now = time.monotonic()
        available = [v for t, values in self.samples if t >= now - self.window
                     for v in values if v is not None]
        ceiling = self.ceiling or max(1, max(available, default=0) * 1.15)
        painter.setPen(QColor('#7a9fae'))
        painter.drawText(QRectF(100, 4, self.width() - 112, 20), Qt.AlignmentFlag.AlignLeft, self.unit)
        painter.drawText(QRectF(12, 4, self.width() - 24, 20), Qt.AlignmentFlag.AlignRight,
                         f'{ceiling:.0f}' if ceiling >= 10 else f'{ceiling:.1f}')
        painter.setPen(QPen(QColor('#163744'), 1))
        for fraction in (0, .5, 1):
            y = plot.bottom() - plot.height() * fraction
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        palette = (getattr(self, 'accent', '#35d9ee'), '#67dfb2')
        for index, name in enumerate(self.series):
            painter.setPen(QPen(QColor(palette[index % 2]), 1.8))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for segment in self.segments(index, now):
                path = QPainterPath()
                for offset, (stamp, value) in enumerate(segment):
                    point = QPointF(plot.right() - (now - stamp) / self.window * plot.width(),
                                    plot.bottom() - max(0, min(1, value / ceiling)) * plot.height())
                    if offset == 0:
                        path.moveTo(point)
                    else:
                        path.lineTo(point)
                if len(segment) == 1:
                    painter.drawEllipse(path.currentPosition(), 2, 2)
                else:
                    painter.drawPath(path)
            painter.drawText(QRectF(12 + index * self.width() / len(self.series),
                                   self.height() - 24, self.width() / len(self.series) - 12, 20),
                             Qt.AlignmentFlag.AlignLeft, name)
        painter.setPen(QColor('#7a9fae'))
        painter.drawText(QRectF(12, plot.bottom() + 3, self.width() - 24, 18), Qt.AlignmentFlag.AlignLeft,
                         '−' + self.range_selector.currentText())
        painter.drawText(QRectF(12, plot.bottom() + 3, self.width() - 24, 18), Qt.AlignmentFlag.AlignRight, 'Now')
        if not available:
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, 'Waiting for readings…')
        if self.hover_x is not None:
            painter.setPen(QPen(QColor('#d5e6ff'), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(self.hover_x, plot.top()), QPointF(self.hover_x, plot.bottom()))


class Meter(QWidget):
    def __init__(self, title, expanded=False, changed=None):
        super().__init__()
        self.setObjectName('cyberMeter')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet('QWidget#cyberMeter {background: #091c26; border: 1px solid #1c3c4a; border-radius: 6px;} QWidget#cyberMeter:hover {border-color: #34869b;}')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 7, 8, 7)
        header = QHBoxLayout()
        self.header_layout = header
        self.toggle = QToolButton()
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setStyleSheet('QToolButton {color: #9eeafa; font-family: Consolas; background: transparent; border: none; padding: 4px 0; font-weight: 700;} QToolButton:hover {color: #ffffff;}')
        self.toggle.setToolTip('Expand details and live history')
        header.addWidget(self.toggle)
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
        self.detail.setStyleSheet('color: #8faebd; font-size: 11px;')
        layout.addWidget(self.detail)
        self.graph = HistoryGraph()
        layout.addWidget(self.graph)
        self.toggle.toggled.connect(lambda value: (self.set_expanded(value), changed(value) if changed else None))
        self.set_expanded(expanded)
        self.update_value(None)

    def set_expanded(self, expanded):
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.graph.setVisible(expanded)
        self.detail.setVisible(expanded)

    def update_value(self, value, detail=''):
        value = number(value)
        shade = '#657086' if value is None else color(value)
        self.value.setText('N/A' if value is None else f'{value:.1f}%')
        self.value.setStyleSheet(f'color: {shade}; font-family: Consolas; font-size: 21px; font-weight: 700;')
        self.bar.setValue(0 if value is None else round(max(0, min(100, value)) * 10))
        self.bar.setStyleSheet('QProgressBar {background: #102c38; border: none; border-radius: 4px;}'
                              f'QProgressBar::chunk {{background: {shade}; border-radius: 4px;}}')
        self.detail.setText(detail)
        self.toggle.setToolTip(detail + '\nClick for details and live history')
        self.graph.add_sample((value,))


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
        self.toggle.setStyleSheet('QToolButton {color: #7be8fa; background: #0a2430; border: 1px solid #1c4352;'
                                 'border-left: 2px solid #35d9ee; border-radius: 4px; padding: 9px; font-family: Consolas; font-size: 11px; text-align: left;}'
                                 'QToolButton:hover {background: #103442; border-color: #35d9ee;}'
                                 'QToolButton:checked {background: #10303c; color: #d9faff;}')
        self.toggle.setSizePolicy(self.sizePolicy().horizontalPolicy(), self.sizePolicy().verticalPolicy())
        self.header_layout = QHBoxLayout()
        self.header_layout.addWidget(self.toggle, 1)
        layout.addLayout(self.header_layout)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(4, 6, 4, 4)
        self.label = QLabel('Waiting for first sample…')
        self.label.setWordWrap(True)
        self.label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setStyleSheet('color: #adcbd7; font-size: 12px;')
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
        painter.setPen(QPen(QColor('#51e0ef'), 2, Qt.PenStyle.SolidLine,
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


def pressure_summary(readings):
    """Describe measured pressure, without claiming to identify a slowdown's cause."""
    notes = []
    for key, limit, message in (
        ('cpu', 85, 'CPU is busy. Check the highest-CPU apps below.'),
        ('ram', 85, 'Memory use is high. Check the largest apps before opening more.'),
        ('gpu', 95, 'GPU is busy. This can be expected in games or graphics work.'),
        ('disk', 90, 'The selected drive is nearly full. Check its free space.')):
        value = number(readings.get(key))
        if value is not None and value >= limit:
            notes.append(message)
    if not any(number(readings.get(key)) is not None for key in ('cpu', 'ram', 'gpu', 'disk')):
        return 'Waiting for current readings…'
    return '\n\n'.join(notes) if notes else 'No high resource use detected in the latest sample.'


def history_rows(graphs, now):
    """Export actual collected samples, keeping unavailable values explicitly empty."""
    rows = []
    for metric, graph in graphs.items():
        for stamp, values in graph.samples:
            if not now - 3600 <= stamp <= now:
                continue
            utc = datetime.fromtimestamp(stamp + graph.clock_offset, timezone.utc).isoformat(timespec='milliseconds')
            for series, value in zip(graph.series, values):
                rows.append((utc, metric, series, '' if value is None else value,
                             graph.unit, 'unavailable' if value is None else 'recorded'))
    return sorted(rows, key=lambda row: (row[0], row[1], row[2]))


def live_sensor_lines(data, known, temperatures=True):
    """Keep discovered sensor labels, but never display stale values as live."""
    current = {}
    for sensor in data.get('hardware', []):
        kind = sensor.get('kind')
        name = str(sensor.get('name', 'Sensor'))
        if kind not in ('fan', 'temperature') or any(word in name.lower() for word in ('warning', 'critical', 'limit', 'threshold')):
            continue
        key = sensor.get('id') or (sensor.get('device', 'Hardware'), kind, name)
        known[key] = dict(sensor)
        if data.get('hardware_age', 0) <= 2:
            current[key] = number(sensor.get('value'))
    lines = []
    for key, label in (('cpu_temp', 'CPU temperature'), ('gpu_temp', 'GPU temperature')):
        value = number(data.get(key)) if temperatures and data.get(key + '_age', 0) <= 2 else None
        lines.append(label + ': ' + fahrenheit(value))
    for key, sensor in sorted(known.items(), key=lambda item: (str(item[1].get('device', '')), str(item[1].get('name', '')))):
        value = current.get(key)
        if sensor['kind'] == 'temperature':
            reading = fahrenheit(value) if temperatures else 'N/A'
        else:
            reading = f'{value:.0f} RPM' if value is not None else 'N/A'
        lines.append(f"{sensor.get('device', 'Hardware')} · {sensor.get('name', 'Sensor')}: {reading}")
    if not any(sensor['kind'] == 'fan' for sensor in known.values()):
        lines.append('Fans: N/A')
    for title, tokens in [('Motherboard temperature', ('lpc', 'motherboard', 'mainboard')),
                           ('Storage temperature', ('nvme', 'hdd', 'ssd', 'storage'))]:
        if not any(sensor['kind'] == 'temperature' and any(token in (str(sensor.get('id', '')) + str(sensor.get('device', ''))).lower()
                   for token in tokens) for sensor in known.values()):
            lines.append(title + ': N/A')
    return lines


def sensor_status(data):
    if not data.get('hardware'):
        return 'SENSORS UNAVAILABLE'
    return 'SENSORS LIVE' if data.get('hardware_age', 0) <= 2 else 'SENSORS DELAYED'


class Monitor(QWidget):
    def __init__(self):
        super().__init__()
        self.root = ROOT
        self.options = load_settings()
        self.alerts = extras.SustainedAlerts()
        self.known_sensors = {}
        self.fps_reader = extras.FpsReader(self)
        self.overlay_window = None
        self.fps_scene = None
        self.hardware_page = None
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
        self.setStyleSheet('QLabel {color: #d9f6ff; font-family: "Segoe UI"; font-size: 13px;}'
                          'QPushButton {color: #b8c4d8; background: transparent; border: none;'
                          'font-size: 20px;} QPushButton:hover {color: #ff6577;}')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 16, 22, 28)
        title = QHBoxLayout()
        label = QLabel('PC // MONITOR')
        label.setStyleSheet('color: #70efff; font-family: Consolas; font-weight: 700; font-size: 16px;')
        title.addWidget(label)
        title.addStretch()
        self.view_button = QPushButton('Widget view')
        self.view_button.setStyleSheet('font-size: 12px; color: #70efff; padding: 5px;')
        self.view_button.clicked.connect(self.toggle_dashboard)
        title.addWidget(self.view_button)
        layout_button = QPushButton('Layout')
        layout_button.setStyleSheet('font-size: 12px; color: #70efff; padding: 5px;')
        layout_menu = extras.QMenu(layout_button)
        layout_menu.addAction('Move panels freely', self.float_panels)
        layout_menu.addAction('Keep panels together', self.dock_panels)
        layout_menu.addAction('Arrange floating panels', self.arrange_panels)
        layout_menu.addAction('Reset dashboard layout', self.reset_layout)
        layout_button.setMenu(layout_menu)
        title.addWidget(layout_button)
        settings = QPushButton('Settings')
        settings.setStyleSheet('font-size: 12px; padding: 5px; color: #51e0ef;')
        settings.setToolTip('Appearance and monitoring settings')
        settings.clicked.connect(self.open_settings)
        title.addWidget(settings)
        close = QPushButton('×')
        close.setFixedSize(26, 26)
        close.setToolTip('Close monitor')
        close.clicked.connect(self.close)
        title.addWidget(close)
        layout.addLayout(title)
        self.alert_banner = QLabel()
        self.alert_banner.setWordWrap(True)
        self.alert_banner.setTextFormat(Qt.TextFormat.PlainText)
        self.alert_banner.setStyleSheet('color: #ffcf86; background: #382412; border: 1px solid #c9863a; padding: 8px; border-radius: 5px;')
        self.alert_banner.hide()
        layout.addWidget(self.alert_banner)
        self.diagnostic_dialog = None
        self.current_readings = {}
        self.current_sensor_data = {}
        self.inspect_button = QPushButton('What’s slowing my PC?  ↗')
        self.inspect_button.setStyleSheet('QPushButton {font-family: Consolas; font-size: 12px; color: #70efff; padding: 10px; background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #0d3442,stop:1 #0a1c29); border: 1px solid #32879b; border-radius: 4px;} QPushButton:hover {background: #164555; color: white; border-color: #70efff;}')
        self.inspect_button.clicked.connect(self.open_diagnostics)
        layout.addWidget(self.inspect_button)
        self.dashboard_toolbar = QWidget()
        tools = QHBoxLayout(self.dashboard_toolbar)
        tools.setContentsMargins(0, 2, 0, 2)
        self.overview = QLabel('Waiting for readings…')
        self.overview.setStyleSheet('color: #9cc6d4; font-family: Consolas; font-size: 11px;')
        self.overview.setWordWrap(True)
        tools.addWidget(self.overview, 1)
        range_label = QLabel('ALL GRAPHS')
        range_label.setStyleSheet('color: #75abba; font-family: Consolas; font-size: 10px;')
        tools.addWidget(range_label)
        self.global_range = QComboBox()
        self.global_range.setAccessibleName('Time range for all graphs')
        self.global_range.setPlaceholderText('Mixed')
        self.global_range.setStyleSheet('QComboBox {color: #b9f4ff; background: #0a2430; padding: 5px; border: 1px solid #24566a;} QComboBox QAbstractItemView {background: #0a2430; color: #d9f6ff;}')
        for text, seconds in [('5 min', 300), ('30 min', 1800), ('1 hour', 3600)]:
            self.global_range.addItem(text, seconds)
        self.global_range.setCurrentIndex(0)
        self.global_range.currentIndexChanged.connect(self.set_all_graph_ranges)
        tools.addWidget(self.global_range)
        export = QPushButton('Export CSV')
        export.setStyleSheet('QPushButton {font-size: 12px; color: #70efff; background: #0a2430; border: 1px solid #24566a; padding: 6px 10px; border-radius: 4px;} QPushButton:hover {background: #164555;}')
        export.setToolTip('Save up to one hour of collected readings. Includes missing values; no process list is exported.')
        export.clicked.connect(self.export_history)
        tools.addWidget(export)
        layout.addWidget(self.dashboard_toolbar)
        self.control_dock = extras.ControlDock(self)
        layout.addWidget(self.control_dock)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet('QScrollArea, QScrollArea > QWidget > QWidget {background: transparent;}'
                                 'QScrollBar:vertical {background: #06131c; width: 7px; border-radius: 3px;}'
                                 'QScrollBar::handle:vertical {background: #246477; min-height: 25px; border-radius: 3px;}'
                                 'QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height: 0px;}')
        content = ReorderList(self)
        content_layout = QVBoxLayout(content)
        self.content_layout = content_layout
        content_layout.setContentsMargins(0, 0, 8, 0)
        self.scroll.setWidget(content)
        layout.addWidget(self.scroll, 1)
        self.meters = {}
        for key, text in [('cpu', 'CPU'), ('ram', 'RAM'), ('disk', 'DISK SPACE'), ('gpu', 'GPU')]:
            meter = Meter(text, self.options['history_' + key],
                          lambda value, name=key: self.save_history(name, value))
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
        self.network_graph = HistoryGraph(('Download', 'Upload'), 'Mbps', ceiling=None)
        self.network_history_adapter = None
        self.sections['network'].body_layout.addWidget(self.network_graph)
        self.network_quality = extras.NetworkQuality(self)
        self.sections['network'].body_layout.addWidget(self.network_quality)
        self.temperature_graph = HistoryGraph(('CPU', 'GPU'), 'Temperature · °F', ceiling=None)
        self.temperature_graph.setFixedHeight(150)
        self.sections['hardware'].body_layout.addWidget(self.temperature_graph)
        for graph in self.history_graphs().values():
            graph.range_selector.currentIndexChanged.connect(self.sync_global_range)
        for key, meter in self.meters.items():
            if key in ('cpu', 'gpu'):
                meter.dial = extras.Dial(self, key)
                meter.dial.setFixedWidth(112)
                meter.visual_row = QWidget()
                visual_layout = QHBoxLayout(meter.visual_row)
                visual_layout.setContentsMargins(0, 0, 0, 0)
                visual_layout.setSpacing(6)
                meter.layout().removeWidget(meter.graph)
                visual_layout.addWidget(meter.dial)
                visual_layout.addWidget(meter.graph, 1)
                meter.layout().addWidget(meter.visual_row)
            meter.card_sizer = extras.CardSizer(self, key, meter.graph)
            meter.layout().addWidget(meter.card_sizer)
        self.core_grid = QFormLayout()
        self.sections['cores'].body_layout.addLayout(self.core_grid)
        content_layout.addStretch()
        self.row_handles = {}
        for key, widget in self.panel_widgets().items():
            handle = RowDragHandle(self, key)
            widget.header_layout.insertWidget(0, handle)
            self.row_handles[key] = handle
        self.dashboard = QWidget()
        dashboard_layout = QHBoxLayout(self.dashboard)
        dashboard_layout.setContentsMargins(0, 0, 0, 0)
        dashboard_layout.setSpacing(18)
        self.dashboard_grid = QGridLayout()
        self.dashboard_grid.setSpacing(12)
        self.dashboard_grid.setColumnStretch(0, 1)
        self.dashboard_grid.setColumnStretch(1, 1)
        main_cards = DashboardGridArea(self)
        main_cards.setLayout(self.dashboard_grid)
        main_scroll = QScrollArea()
        self.dashboard_scroll = main_scroll
        main_scroll.setWidgetResizable(True)
        main_scroll.setFrameShape(QFrame.Shape.NoFrame)
        main_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        main_scroll.setStyleSheet(self.scroll.styleSheet())
        main_scroll.setWidget(main_cards)
        self.sidebar_layout = QVBoxLayout()
        self.sidebar_layout.setSpacing(12)
        self.sidebar_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        sidebar = QWidget()
        sidebar.setLayout(self.sidebar_layout)
        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setFrameShape(QFrame.Shape.NoFrame)
        side_scroll.setStyleSheet(self.scroll.styleSheet())
        side_scroll.setWidget(sidebar)
        side_scroll.setMinimumWidth(270)
        side_scroll.setMaximumWidth(340)
        dashboard_layout.addWidget(main_scroll, 2)
        dashboard_layout.addWidget(side_scroll, 1)
        layout.addWidget(self.dashboard, 1)
        self.panel_controls = QWidget()
        controls_layout = QVBoxLayout(self.panel_controls)
        controls_layout.setContentsMargins(0, 4, 0, 4)
        help_text = QLabel('Drag each panel by its title.\nChoose panels in Settings → Monitoring.')
        help_text.setStyleSheet('color: #8faebd; font-size: 12px;')
        controls_layout.addWidget(help_text)
        buttons = QHBoxLayout()
        arrange = QPushButton('Arrange panels')
        together = QPushButton('Keep together')
        for button in (arrange, together):
            button.setStyleSheet('font-size: 12px; color: #51e0ef; padding: 6px; background: #0a2430; border-radius: 5px;')
            buttons.addWidget(button)
        arrange.clicked.connect(self.arrange_panels)
        together.clicked.connect(self.dock_panels)
        controls_layout.addLayout(buttons)
        layout.addWidget(self.panel_controls)
        self.previous_counters = None
        self.network_sampler = NetworkSampler()
        self.counter_time = time.monotonic()
        self.status = QLabel('Starting sensors…  •  Drag to move')
        self.status.setStyleSheet('color: #79a7b7; font-family: Consolas; font-size: 10px; padding-top: 6px; border-top: 1px solid #1b3c49;')
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
        if self.options['dashboard']:
            self.keep_dashboard_on_screen()

    def keep_dashboard_on_screen(self):
        area = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        self.move(max(area.left(), min(self.x(), area.right() - self.width() + 1)),
                  max(area.top(), min(self.y(), area.bottom() - self.height() + 1)))

    def toggle_dashboard(self):
        self.save_settings()
        self.options['dashboard'] = not self.options['dashboard']
        self.apply_settings()
        self.keep_dashboard_on_screen()
        self.save_settings()

    def history_graphs(self):
        return {**{key: meter.graph for key, meter in self.meters.items()}, 'network': self.network_graph,
                'temperature': self.temperature_graph, 'latency': self.network_quality.graph}

    def open_hardware(self):
        if self.hardware_page is not None and self.hardware_page.isVisible():
            self.hardware_page.raise_()
            return
        if self.hardware_page is not None:
            self.hardware_page.deleteLater()
        self.hardware_page = extras.HardwarePage(self)
        extras.apply_theme(self)
        self.hardware_page.show()

    def open_overlay(self):
        dialog = extras.OverlaySetup(self)
        extras.apply_theme(self)
        dialog.show()

    def start_fps_test(self):
        if self.fps_scene is None or not self.fps_scene.isVisible():
            if self.fps_scene is not None:
                self.fps_scene.deleteLater()
            self.fps_scene = extras.create_fps_test(self)
        if self.fps_scene is None:
            return
        self.fps_scene.raise_()
        self.fps_reader.start_test()
        self.show_overlay()

    def show_overlay(self):
        if self.overlay_window is not None and self.overlay_window.isVisible():
            self.overlay_window.raise_()
            return
        if self.overlay_window is not None:
            self.overlay_window.deleteLater()
        self.overlay_window = extras.GamingOverlay(self)
        self.overlay_window.show()

    def set_all_graph_ranges(self, index):
        if index < 0:
            return
        for graph in self.history_graphs().values():
            graph.range_selector.setCurrentIndex(index)

    def sync_global_range(self, _index):
        ranges = {graph.range_selector.currentIndex() for graph in self.history_graphs().values()}
        self.global_range.blockSignals(True)
        self.global_range.setCurrentIndex(next(iter(ranges)) if len(ranges) == 1 else -1)
        self.global_range.blockSignals(False)

    def export_history(self):
        rows = history_rows(self.history_graphs(), time.monotonic())
        if not any(row[-1] == 'recorded' for row in rows):
            QMessageBox.information(self, 'Export readings', 'Wait for readings to collect before exporting.')
            return
        filename, _ = QFileDialog.getSaveFileName(self, 'Save recorded readings',
                                                 str(ROOT / 'PC Monitor readings.csv'), 'CSV files (*.csv)')
        if not filename:
            return
        try:
            with open(filename, 'w', newline='', encoding='utf-8-sig') as target:
                writer = csv.writer(target)
                writer.writerow(('timestamp_utc', 'metric', 'series', 'value', 'unit', 'status'))
                writer.writerows(rows)
        except OSError as error:
            QMessageBox.warning(self, 'Export readings', 'Could not save the file: ' + str(error))
            return
        QMessageBox.information(self, 'Readings saved', f'Saved {len(rows):,} readings. Times are UTC; history covers only samples collected during this run.')

    def save_history(self, name, value):
        self.options['history_' + name] = value
        self.save_settings()

    def open_diagnostics(self):
        if self.diagnostic_dialog is not None:
            self.diagnostic_dialog.show()
            self.diagnostic_dialog.raise_()
            self.diagnostic_dialog.activateWindow()
            return
        dialog = QDialog(self)
        self.diagnostic_dialog = dialog
        dialog.setWindowTitle('PC Monitor · Resource check')
        dialog.resize(480, 560)
        dialog.setMinimumWidth(360)
        dialog.setStyleSheet('QDialog {background: #07151e;} QLabel {color: #d9f6ff; font-size: 13px;} QPushButton {font-size: 13px; color: #70efff; padding: 8px;}')
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 20, 20, 20)
        heading = QLabel('WHAT’S USING YOUR PC?')
        heading.setStyleSheet('font-size: 17px; font-weight: 700;')
        layout.addWidget(heading)
        hint = QLabel('Live readings show resource use, not proof of what caused a slowdown. High use can be normal during demanding work.')
        hint.setWordWrap(True)
        hint.setStyleSheet('color: #8faebd; font-size: 12px;')
        layout.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        scroll.setWidget(content)
        scroll.setStyleSheet('QScrollArea, QScrollArea > QWidget > QWidget {background: transparent;}')
        layout.addWidget(scroll, 1)
        fields = []
        for title in ('CURRENT READINGS', 'RESOURCE PRESSURE', 'HIGHEST CPU · TOTAL CAPACITY', 'LARGEST APPS · WORKING SET'):
            label = QLabel(title)
            label.setStyleSheet('color: #51e0ef; font-size: 11px; font-weight: 600; margin-top: 10px;')
            body.addWidget(label)
            field = QLabel()
            field.setWordWrap(True)
            field.setTextFormat(Qt.TextFormat.PlainText)
            field.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body.addWidget(field)
            fields.append(field)
        body.addStretch()
        footer = QLabel('Disk space is capacity, not disk busy time. GPU may be unavailable without its sensor provider. Protected apps may be missing. Updates follow your refresh setting.')
        footer.setWordWrap(True)
        footer.setStyleSheet('color: #8faebd; font-size: 11px;')
        layout.addWidget(footer)
        close = QPushButton('Close')
        close.clicked.connect(dialog.close)
        layout.addWidget(close)

        def refresh_report():
            readings, data = self.current_readings, self.current_sensor_data
            fields[0].setText('   ·   '.join(
                name + ' ' + ('N/A' if number(readings.get(key)) is None else f'{readings[key]:.1f}%')
                for key, name in [('cpu', 'CPU'), ('ram', 'RAM'), ('gpu', 'GPU'), ('disk', 'Disk space')]))
            fields[1].setText(pressure_summary(readings))
            fields[2].setText('\n'.join(f"{row['name']} — {row['cpu']:.1f}%" for row in data.get('top_cpu', []))
                              or 'Waiting for process samples…')
            fields[3].setText('\n'.join(f"{row['name']} — {row['ram'] / 2**20:.0f} MiB" for row in data.get('top_ram', []))
                              or 'Waiting for process samples…')

        timer = QTimer(dialog)
        timer.timeout.connect(refresh_report)
        timer.start(1000)
        refresh_report()
        dialog.finished.connect(lambda result: (timer.stop(), setattr(self, 'diagnostic_dialog', None), dialog.deleteLater()))
        dialog.show()

    def save_settings(self):
        self.options.update(x=self.x(), y=self.y())
        if self.options['dashboard'] and not self.options['separate_panels']:
            self.options.update(dashboard_width=self.width(), dashboard_height=self.height())
        elif not self.options['separate_panels']:
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
        if self.options['separate_panels']:
            return
        if self.options['dashboard']:
            while self.dashboard_grid.count():
                self.dashboard_grid.takeAt(0)
            row, column = 0, 0
            for key in self.options['dashboard_order']:
                if not self.options['show_' + key]:
                    continue
                span = self.options['card_spans'].get(key, 1)
                if span == 2 and column:
                    row, column = row + 1, 0
                self.dashboard_grid.addWidget(widgets[key], row, column, 1, span)
                column += span
                if column >= 2:
                    row, column = row + 1, 0
            if column:
                row += 1
            self.dashboard_grid.addWidget(widgets['network'], row, 0, 1, 2)
            for key in ('apps', 'hardware', 'activity', 'cores', 'graphics', 'battery'):
                self.sidebar_layout.addWidget(widgets[key])
            self.sidebar_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
            self.dashboard_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
            return
        for index, key in enumerate(self.options['monitor_order']):
            self.content_layout.insertWidget(index, widgets[key])

    def move_monitor(self, key, before=None):
        if self.options['dashboard'] and key in self.meters:
            order = self.options['dashboard_order']
            if before == key:
                return
            order.remove(key)
            order.insert(order.index(before) if before in order else len(order), key)
            self.apply_order()
            self.save_settings()
            return
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
        self.options['floating_enabled'] = False
        self.options['separate_panels'] = False
        self.apply_settings()
        self.save_settings()

    def float_panels(self):
        self.options['floating_enabled'] = True
        self.options['separate_panels'] = True
        self.apply_settings()
        self.save_settings()

    def reset_layout(self):
        self.options.update(dashboard_order=['cpu', 'gpu', 'ram', 'disk'], card_heights={},
                            card_spans={}, panel_positions={})
        if self.options['separate_panels']:
            self.arrange_panels()
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
        dashboard = self.options['dashboard']
        self.dashboard_toolbar.setVisible(dashboard and not self.options['separate_panels'])
        self.control_dock.setVisible(dashboard and not self.options['separate_panels'])
        self.view_button.setVisible(not self.options['separate_panels'])
        self.view_button.setText('Widget view' if dashboard else 'Dashboard')
        self.view_button.setToolTip('Switch between the dashboard and compact widget')
        self.dashboard.setVisible(dashboard and not self.options['separate_panels'])
        self.scroll.setVisible(not dashboard and not self.options['separate_panels'])
        for key, handle in self.row_handles.items():
            handle.setVisible(not self.options['separate_panels'] and (not dashboard or key in self.meters))
            handle.setToolTip('Drag onto another card to reorder' if dashboard else 'Drag up or down to reorder this item')
        for key, meter in self.meters.items():
            meter.setVisible(self.options['show_' + key])
            meter.graph.setFixedHeight(self.options['card_heights'].get(key, 105) if dashboard else 155)
            meter.card_sizer.setVisible(dashboard)
            if hasattr(meter, 'dial'):
                meter.dial.setVisible(dashboard and self.options['gauges'])
                meter.bar.setVisible(not dashboard or not self.options['gauges'])
                meter.value.setVisible(not dashboard or not self.options['gauges'])
            if dashboard and not getattr(self, 'dashboard_applied', False):
                meter.toggle.blockSignals(True)
                meter.toggle.setChecked(True)
                meter.toggle.blockSignals(False)
            elif not dashboard:
                meter.toggle.blockSignals(True)
                meter.toggle.setChecked(self.options['history_' + key])
                meter.toggle.blockSignals(False)
            meter.set_expanded(meter.toggle.isChecked())
            meter.detail.setVisible(meter.toggle.isChecked() and not self.options['compact'])
        for key, section in self.sections.items():
            section.setVisible(self.options['show_' + key])
            if dashboard and not getattr(self, 'dashboard_applied', False):
                expanded = key in ('network', 'apps', 'hardware')
                section.toggle.blockSignals(True)
                section.toggle.setChecked(expanded)
                section.toggle.blockSignals(False)
                section.set_expanded(expanded)
            elif not dashboard:
                option = {'cores': 'cpu', 'graphics': 'gpu'}.get(key, key)
                expanded = self.options['expand_' + option]
                section.toggle.blockSignals(True)
                section.toggle.setChecked(expanded)
                section.toggle.blockSignals(False)
                section.set_expanded(expanded)
        self.network_graph.setFixedHeight(105 if dashboard else 155)
        if self.options['separate_panels']:
            for key, section in self.sections.items():
                section.body.show()
                section.toggle.hide()
        self.dashboard_applied = dashboard
        self.setMinimumWidth(900 if dashboard and not self.options['separate_panels'] else 320)
        self.layout().activate()
        minimum = self.minimumSizeHint()
        area = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        width = min(self.options['dashboard_width'], area.width()) if dashboard else self.options['width']
        height = min(self.options['dashboard_height'], area.height()) if dashboard else (self.options['height'] or 600)
        if self.options['separate_panels']:
            width, height = 440, 300
        self.resize(max(width, minimum.width()),
                    max(190 if self.options['separate_panels'] else height,
                        minimum.height()))
        self.update()
        if visible:
            self.show()
        extras.apply_theme(self)

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
        cpu_usage = None if first else psutil.cpu_percent(None)
        disk_usage = None
        self.meters['cpu'].update_value(cpu_usage,
                                        ('Temperature: ' + fahrenheit(data.get('cpu_temp'))
                                         + (' (last reading)' if data.get('cpu_temp_age', 0) > 2 else ''))
                                        if self.options['temperatures'] else 'Live processor usage')
        mem = psutil.virtual_memory()
        self.meters['ram'].update_value(mem.percent, f'{mem.used / 2**30:.1f} / {mem.total / 2**30:.1f} GB used')
        try:
            disk = psutil.disk_usage(self.drive)
            disk_usage = disk.percent
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
        self.current_readings = dict(cpu=cpu_usage, ram=mem.percent, disk=disk_usage,
                                     gpu=data.get('gpu') if data.get('gpu_age', 0) <= 2 else None)
        self.current_sensor_data = data
        alert_values = dict(self.current_readings)
        for key in ('cpu_temp', 'gpu_temp'):
            value = number(data.get(key))
            alert_values[key] = value * 1.8 + 32 if value is not None and data.get(key + '_age', 0) <= 2 else None
        new_alerts = self.alerts.update(alert_values, self.options)
        if new_alerts and self.options['alert_sound']:
            QApplication.beep()
        self.alert_banner.setText('ALERT · ' + '\n'.join(self.alerts.active.values()))
        self.alert_banner.setVisible(bool(self.alerts.active))
        self.temperature_graph.add_sample(tuple(
            value * 1.8 + 32 if self.options['temperatures'] and (value := number(data.get(key + '_temp'))) is not None
            and data.get(key + '_temp_age', 0) <= 2 else None for key in ('cpu', 'gpu')))
        for key in ('cpu', 'gpu'):
            temperature = data.get(key + '_temp') if self.options['temperatures'] else None
            self.meters[key].dial.set_reading(self.current_readings.get(key), temperature)
        pressure = 'MEMORY HIGH' if mem.percent >= 85 else 'CPU BUSY' if cpu_usage is not None and cpu_usage >= 85 else 'MONITORING'
        self.overview.setText(pressure + '  /  ' + sensor_status(data))
        self.overview.setToolTip(pressure_summary(self.current_readings) + '\nSensor status refers to the external hardware sensor provider.')
        self.refresh_details(data, first)
        extras.apply_theme(self)

    def refresh_details(self, data, first=False):
        now = time.monotonic()
        counters = {}
        network = self.network_sampler.sample(self.options['network_adapter'])
        if network['adapter'] != self.network_history_adapter:
            self.network_graph.samples.clear()
            self.network_history_adapter = network['adapter']
        self.network_graph.add_sample(tuple(
            None if network[key] is None else network[key] * 8 / 1_000_000
            for key in ('download', 'upload')))
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
        try:
            adapter = psutil.net_if_stats().get(network['adapter'])
            if adapter is not None:
                self.sections['network'].label.setText(self.sections['network'].label.text() +
                    ('\nLink speed: ' + str(adapter.speed) + ' Mbps (adapter)' if adapter.speed > 0 else '\nLink speed: unavailable'))
        except (OSError, psutil.Error):
            pass
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

        hardware_lines = live_sensor_lines(data, self.known_sensors, self.options['temperatures'])
        self.sections['hardware'].label.setText('\n'.join(hardware_lines))
        self.sections['hardware'].label.setToolTip(
            'Live temperatures and fan speeds from available sensors. Missing or older-than-2-second readings show N/A.\n'
            'Warning and critical reference limits are excluded because they are not current temperatures.')

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
        # Wrapped sensor/process text must grow its scroll content instead of
        # being squeezed to a one-line minimum by the dashboard's side column.
        for section in self.sections.values():
            label = section.label
            label.setMinimumHeight(max(0, label.heightForWidth(max(1, label.width()))))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        background = QLinearGradient(0, 0, self.width(), self.height())
        alpha = round(self.options['opacity'] * 2.55)
        background.setColorAt(0, QColor(5, 21, 30, alpha))
        background.setColorAt(1, QColor(4, 11, 20, alpha))
        if self.options['accent'] in extras.THEMES:
            _, base, panel, _ = extras.theme_colors(self)
            for stop, color in ((0, panel), (1, base)):
                shade = QColor(color)
                shade.setAlpha(alpha)
                background.setColorAt(stop, shade)
        painter.setBrush(background)
        painter.setPen(QPen(QColor('#245365'), 1))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)
        painter.setPen(QPen(QColor(44, 151, 177, 15), 1))
        for x in range(16, self.width() - 8, 24):
            painter.drawLine(x, 48, x, self.height() - 26)
        for y in range(48, self.height() - 20, 24):
            painter.drawLine(9, y, self.width() - 9, y)
        accent = QColor(extras.ACCENTS.get(self.options['accent'], '#35d9ee'))
        intensity = self.options['glow']
        if intensity:
            glow_color = QColor(accent)
            glow_color.setAlpha(round(intensity * 0.8))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(glow_color, 6))
            painter.drawRoundedRect(self.rect().adjusted(4, 4, -4, -4), 10, 10)
        painter.setPen(QPen(accent, 2))
        for x, y, dx, dy in [(9, 9, 1, 1), (self.width()-9, 9, -1, 1),
                             (9, self.height()-9, 1, -1), (self.width()-9, self.height()-9, -1, -1)]:
            painter.drawLine(x, y, x + dx * 16, y)
            painter.drawLine(x, y, x, y + dy * 16)

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
        self.network_quality.shutdown()
        self.fps_reader.stop()
        if self.fps_scene is not None:
            self.fps_scene.close()
        if self.overlay_window is not None:
            self.overlay_window.close()
        if self.hardware_page is not None:
            self.hardware_page.close()
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
    acquired = lock.tryLock(0)
    if not acquired and '--fps-admin-restart' in sys.argv:
        deadline = time.monotonic() + 8
        while not acquired and time.monotonic() < deadline:
            time.sleep(.1)
            acquired = lock.tryLock(0)
    if not acquired:
        client = QLocalSocket()
        client.connectToServer(server_name)
        if client.waitForConnected(1500):
            client.disconnectFromServer()
        sys.exit(0)
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
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
    if '--fps-test' in sys.argv:
        QTimer.singleShot(700, widget.start_fps_test)
    result = app.exec()
    server.close()
    lock.unlock()
    sys.exit(result)
