"""Local dashboard controls, hardware inventory and optional PresentMon overlay."""
import csv
import ctypes
import json
import math
import os
import platform
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import psutil
from PySide6.QtCore import Qt, QTimer, QObject, QProcess, QRectF, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PySide6.QtWidgets import (QWidget, QLabel, QPushButton, QHBoxLayout, QVBoxLayout,
                               QDialog, QComboBox, QScrollArea, QFrame, QFileDialog,
                               QMessageBox, QSpinBox, QCheckBox, QMenu, QApplication)

ACCENTS = {'Cyan': '#35d9ee', 'Violet': '#bb91ff', 'Green': '#67dfb2', 'Amber': '#ffbe62'}
THEMES = {
    'NASA Mission Control': ('#77b8ff', '#071426', '#102744', '#e6f1ff'),
    'Matrix': ('#5cff85', '#030d07', '#0b2413', '#c5ffd1'),
    'Amber CRT': ('#ffbc59', '#160f06', '#2d200d', '#ffebca'),
    'Deep Space': ('#c29bff', '#10091f', '#25163c', '#efe3ff'),
}
ACCENTS.update({name: colors[0] for name, colors in THEMES.items()})


def theme_colors(owner):
    return THEMES.get(owner.options.get('accent'),
                      (ACCENTS.get(owner.options.get('accent'), ACCENTS['Cyan']), '#07151e', '#0a2430', '#d9f6ff'))


def finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


class SustainedAlerts:
    """Require continuous high samples; alert once, rearm after recovery."""
    def __init__(self):
        self.pending, self.active, self.last_fired = {}, {}, {}
        self.last_sample = None
        self.history = deque(maxlen=30)

    def update(self, values, options, now=None):
        now = time.monotonic() if now is None else now
        if not options.get('alerts_enabled', False):
            self.pending.clear()
            self.active.clear()
            self.last_sample = None
            return []
        if self.last_sample is not None and now - self.last_sample > max(15, options.get('refresh', 1) * 2):
            self.pending.clear()
            self.active.clear()
        self.last_sample = now
        fired = []
        for key, label, unit in [('cpu', 'CPU use', '%'), ('ram', 'Memory use', '%'),
                                 ('cpu_temp', 'CPU temperature', '°F'), ('gpu_temp', 'GPU temperature', '°F')]:
            value = finite(values.get(key))
            limit = options['alert_' + key]
            if value is None or value < limit - 2:
                self.pending.pop(key, None)
                self.active.pop(key, None)
                continue
            if value < limit:
                self.pending.pop(key, None)
                continue
            self.pending.setdefault(key, now)
            if now - self.pending[key] >= options['alert_seconds']:
                message = f'{label}: {value:.1f}{unit} (limit {limit}{unit})'
                if key not in self.active and now - self.last_fired.get(key, -math.inf) >= 300:
                    self.last_fired[key] = now
                    fired.append(message)
                    self.history.appendleft(time.strftime('%H:%M:%S') + ' · ' + message)
                self.active[key] = message
        return fired


class Dial(QWidget):
    def __init__(self, owner, key):
        super().__init__()
        self.owner, self.key = owner, key
        self.value, self.temperature = None, None
        self.setMinimumHeight(116)
        self.setAccessibleName(key.upper() + ' circular usage gauge')

    def set_reading(self, value, temperature):
        self.value, self.temperature = finite(value), finite(temperature)
        self.setAccessibleDescription('Usage ' + ('unavailable' if self.value is None else f'{self.value:.1f}%'))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        diameter = min(100, self.height() - 12, self.width() - 12)
        rect = QRectF((self.width() - diameter) / 2, 6, diameter, diameter)
        p.setPen(QPen(QColor('#14313e'), 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, 225 * 16, -270 * 16)
        accent = QColor(ACCENTS.get(self.owner.options.get('accent'), ACCENTS['Cyan']))
        if self.value is not None:
            shade = QColor('#ff6577') if self.value >= 80 else QColor('#ffd166') if self.value >= 50 else accent
            p.setPen(QPen(shade, 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(rect, 225 * 16, -round(270 * 16 * max(0, min(100, self.value)) / 100))
        p.setPen(QColor('#e5f8ff'))
        font = p.font()
        font.setFamily('Consolas')
        font.setPointSize(16)
        font.setBold(True)
        p.setFont(font)
        p.drawText(rect.adjusted(0, -10, 0, 0), Qt.AlignmentFlag.AlignCenter,
                   'N/A' if self.value is None else f'{self.value:.1f}%')
        font.setPointSize(9)
        font.setBold(False)
        p.setFont(font)
        p.setPen(accent)
        p.drawText(rect.adjusted(0, 30, 0, 0), Qt.AlignmentFlag.AlignCenter,
                   'TEMP N/A' if self.temperature is None else f'{self.temperature * 1.8 + 32:.1f} °F')


class CardSizer(QWidget):
    def __init__(self, owner, key, graph):
        super().__init__()
        self.owner, self.key, self.graph = owner, key, graph
        self.start = None
        self.setFixedHeight(12)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setToolTip('Drag up/down to resize the graph. Right-click for half-width or full-width cards.')
        self.setAccessibleName('Resize ' + key.upper() + ' card')

    def paintEvent(self, event):
        p = QPainter(self)
        p.setPen(QPen(QColor(ACCENTS.get(self.owner.options.get('accent'), ACCENTS['Cyan'])), 2))
        p.drawLine(self.width() // 2 - 15, 6, self.width() // 2 + 15, 6)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            menu = QMenu(self)
            half = menu.addAction('Half-width card')
            full = menu.addAction('Full-width card')
            chosen = menu.exec(event.globalPosition().toPoint())
            if chosen in (half, full):
                self.owner.options['card_spans'][self.key] = 2 if chosen is full else 1
                self.owner.apply_order()
                self.owner.save_settings()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.start = (event.globalPosition().y(), self.graph.height())
            event.accept()

    def mouseMoveEvent(self, event):
        if self.start is not None:
            height = max(90, min(300, round(self.start[1] + event.globalPosition().y() - self.start[0])))
            self.graph.setFixedHeight(height)
            self.owner.options['card_heights'][self.key] = height
            event.accept()

    def mouseReleaseEvent(self, event):
        if self.start is not None:
            self.owner.save_settings()
        self.start = None
        event.accept()


def media_key(action):
    keys = {'down': 0xAE, 'up': 0xAF, 'mute': 0xAD, 'play': 0xB3}
    if os.name != 'nt' or action not in keys:
        raise OSError('Media controls are available on Windows.')
    key = keys[action]
    ctypes.windll.user32.keybd_event(key, 0, 0, 0)
    ctypes.windll.user32.keybd_event(key, 0, 2, 0)


class ControlDock(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        for title, callback in [('Vol −', lambda: media_key('down')), ('Mute', lambda: media_key('mute')),
                                ('Vol +', lambda: media_key('up')), ('Play / pause', lambda: media_key('play')),
                                ('Audio output', lambda: os.startfile('ms-settings:sound')),
                                ('Task Manager', self.task_manager), ('Hardware', owner.open_hardware),
                                ('Gaming overlay', owner.open_overlay)]:
            button = QPushButton(title)
            button.setStyleSheet('QPushButton {font-size: 11px; color: #a3eaf6; background: #0a2430; padding: 7px; border: 1px solid #24566a; border-radius: 4px;} QPushButton:hover {background: #164555;}')
            button.clicked.connect(lambda _checked=False, call=callback: self.run(call))
            row.addWidget(button)
        layout.addLayout(row)
        self.favorites = QHBoxLayout()
        layout.addLayout(self.favorites)
        self.rebuild_favorites()

    def run(self, callback):
        try:
            callback()
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, 'Control dock', str(error))

    def task_manager(self):
        system = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'Taskmgr.exe'
        subprocess.Popen([str(system)])

    def rebuild_favorites(self):
        while self.favorites.count():
            item = self.favorites.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for path in self.owner.options.get('favorite_apps', [])[:6]:
            button = QPushButton(Path(path).stem)
            button.setToolTip(path + '\nRight-click to remove this favorite')
            button.setStyleSheet('font-size: 11px; color: #9cc6d4; padding: 4px;')
            button.clicked.connect(lambda _checked=False, app=path: self.run(lambda: subprocess.Popen([app])))
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(lambda _pos, app=path: self.remove_favorite(app))
            self.favorites.addWidget(button)
        add = QPushButton('+ Favorite app')
        add.setStyleSheet('font-size: 11px; color: #70efff; padding: 4px;')
        add.clicked.connect(self.add_favorite)
        self.favorites.addWidget(add)
        self.favorites.addStretch()

    def add_favorite(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose an application', '', 'Applications (*.exe)')
        if path and Path(path).suffix.lower() == '.exe' and Path(path).is_file():
            apps = self.owner.options['favorite_apps']
            if path not in apps and len(apps) < 6:
                apps.append(path)
                self.owner.save_settings()
                self.rebuild_favorites()

    def remove_favorite(self, path):
        self.owner.options['favorite_apps'].remove(path)
        self.owner.save_settings()
        self.rebuild_favorites()


def ping_summary(samples):
    replies = [finite(value) for value in samples]
    received = [value for value in replies if value is not None and value >= 0]
    differences = [abs(a - b) for a, b in zip(replies, replies[1:])
                   if a is not None and b is not None and a >= 0 and b >= 0]
    return dict(ping=sum(received) / len(received) if received else None,
                jitter=sum(differences) / len(differences) if differences else None,
                loss=100 * (len(samples) - len(received)) / len(samples) if samples else None)


class NetworkQuality(QWidget):
    """User-triggered, asynchronous quality and bounded HTTPS throughput checks."""
    def __init__(self, owner):
        super().__init__()
        self.reply = None
        self.phase = None
        self.download = None
        self.received = 0
        self.manager = QNetworkAccessManager(self)
        self.ping_process = QProcess(self)
        self.ping_process.finished.connect(self.finish_quality)
        self.ping_process.errorOccurred.connect(lambda _: self.quality.setText('Quality check could not run.'))
        self.timeout = QTimer(self)
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(self.cancel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        self.quality = QLabel('NETWORK QUALITY · run a check to measure ping, jitter and loss')
        self.quality.setWordWrap(True)
        self.quality.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.quality)
        row = QHBoxLayout()
        self.target = QComboBox()
        self.target.addItem('Cloudflare', '1.1.1.1')
        self.target.addItem('Google', '8.8.8.8')
        row.addWidget(self.target)
        self.check = QPushButton('Check quality')
        self.check.clicked.connect(self.start_quality)
        row.addWidget(self.check)
        self.speed = QPushButton('Speed test')
        self.speed.clicked.connect(self.start_speed)
        row.addWidget(self.speed)
        self.stop_button = QPushButton('Stop')
        self.stop_button.clicked.connect(self.cancel)
        row.addWidget(self.stop_button)
        layout.addLayout(row)
        self.speed_result = QLabel('Download —   Upload —\nQuick test via Cloudflare · up to 25 MB · runs only when clicked')
        self.speed_result.setWordWrap(True)
        self.speed_result.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.speed_result)
        row = QHBoxLayout()
        self.auto = QCheckBox('Monitor quality every 30s')
        row.addWidget(self.auto)
        copy = QPushButton('Copy results')
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.quality.text() + '\n' + self.speed_result.text()))
        row.addWidget(copy)
        layout.addLayout(row)
        self.graph = owner.network_graph.__class__(('Ping',), 'Latency · ms', ceiling=None)
        self.graph.setFixedHeight(150)
        layout.addWidget(self.graph)
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(lambda: self.start_quality() if self.auto.isChecked() else None)
        self.auto_timer.start(30000)

    def set_busy(self, busy):
        self.check.setEnabled(not busy)
        self.speed.setEnabled(not busy)
        self.target.setEnabled(not busy)

    def start_quality(self):
        if not self.check.isEnabled():
            return
        self.set_busy(True)
        self.quality_target = self.target.currentText()
        self.quality.setText(f'Checking {self.quality_target} · 10 pings…')
        address = self.target.currentData()
        script = "$p=New-Object System.Net.NetworkInformation.Ping; try { $r=@(1..10 | ForEach-Object { try {$x=$p.Send('" + address + "',1000); if($x.Status -eq 'Success'){[double]$x.RoundtripTime}else{$null}}catch{$null}; Start-Sleep -Milliseconds 150 }); ConvertTo-Json -InputObject $r -Compress } finally {$p.Dispose()}"
        self.ping_process.start('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script])
        self.timeout.start(20000)

    def finish_quality(self, code, _status):
        self.timeout.stop()
        self.set_busy(False)
        try:
            samples = json.loads(bytes(self.ping_process.readAllStandardOutput()).decode('utf-8-sig'))
            if code or not isinstance(samples, list) or len(samples) != 10:
                raise ValueError()
            result = ping_summary(samples)
            def ms(key):
                return 'N/A' if result[key] is None else f'{result[key]:.1f} ms'
            self.quality.setText(f'{self.quality_target} · Ping {ms("ping")} · Jitter {ms("jitter")} · Loss {result["loss"]:.0f}%\nUpdated {time.strftime("%H:%M:%S")} · 10 ICMP probes; blocked pings can look like loss.')
            self.graph.add_sample((result['ping'],))
        except (ValueError, TypeError):
            self.quality.setText('Quality check unavailable or stopped. Try the other target.')
            self.graph.add_sample((None,))

    def start_speed(self):
        if not self.speed.isEnabled():
            return
        self.set_busy(True)
        self.download = None
        self.begin_transfer('download')

    def begin_transfer(self, phase):
        self.phase = phase
        self.received = 0
        size = 20_000_000 if phase == 'download' else 5_000_000
        url = f'https://speed.cloudflare.com/__down?bytes={size}&cache={time.time_ns()}' if phase == 'download' else 'https://speed.cloudflare.com/__up'
        request = QNetworkRequest(QUrl(url))
        request.setTransferTimeout(15000)
        request.setRawHeader(b'Cache-Control', b'no-store')
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, 'application/octet-stream')
        self.started = time.monotonic()
        self.speed_result.setText(f'Testing {phase}… · Cloudflare · Stop cancels')
        self.reply = self.manager.get(request) if phase == 'download' else self.manager.post(request, b'0' * size)
        self.reply.readyRead.connect(self.drain)
        self.reply.finished.connect(self.finish_transfer)
        self.timeout.start(20000)

    def drain(self):
        if self.reply is not None:
            self.received += len(self.reply.readAll())

    def finish_transfer(self):
        reply = self.reply
        if reply is None:
            return
        self.drain()
        self.reply = None
        self.timeout.stop()
        duration = time.monotonic() - self.started
        error = reply.error() != QNetworkReply.NetworkError.NoError
        detail = reply.errorString()
        reply.deleteLater()
        if error or duration <= 0 or (self.phase == 'download' and self.received != 20_000_000):
            self.speed_result.setText('Speed test failed: ' + (detail if error else 'Incomplete download.'))
            self.set_busy(False)
            return
        if self.phase == 'download':
            self.download = self.received * 8 / duration / 1_000_000
            self.begin_transfer('upload')
        else:
            upload = 5_000_000 * 8 / duration / 1_000_000
            self.speed_result.setText(f'Download {self.download:.1f} Mbps   Upload {upload:.1f} Mbps\n{time.strftime("%H:%M:%S")} · Cloudflare · quick single-connection estimate, including connection overhead')
            self.set_busy(False)

    def cancel(self):
        self.timeout.stop()
        if self.reply is not None:
            reply, self.reply = self.reply, None
            reply.abort()
            reply.deleteLater()
            self.speed_result.setText('Speed test stopped.')
        if self.ping_process.state() != QProcess.ProcessState.NotRunning:
            self.ping_process.kill()
        self.set_busy(False)

    def shutdown(self):
        self.auto_timer.stop()
        self.auto.setChecked(False)
        self.cancel()
        self.ping_process.waitForFinished(1000)


class HardwarePage(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.details = None
        self.setWindowTitle('PC Monitor · Hardware')
        self.resize(640, 570)
        self.setStyleSheet('QDialog {background: #07151e;} QLabel {color: #d9f6ff; font-size: 13px;} QPushButton {color: #70efff; padding: 7px;}')
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet('QScrollArea, QLabel {background: #091c26;}')
        self.text = QLabel('Reading hardware inventory…')
        self.text.setTextFormat(Qt.TextFormat.PlainText)
        self.text.setWordWrap(True)
        self.text.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text.setMargin(14)
        scroll.setWidget(self.text)
        layout.addWidget(scroll)
        close = QPushButton('Close')
        close.clicked.connect(self.close)
        layout.addWidget(close)
        threading.Thread(target=self.inventory, daemon=True).start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.render)
        self.timer.start(1000)
        self.render()

    def inventory(self):
        if os.name != 'nt':
            self.details = 'Detailed Windows inventory is unavailable on this system.'
            return
        script = "[pscustomobject]@{CPU=@(Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors);GPU=@(Get-CimInstance Win32_VideoController | Select-Object Name);RAM=@(Get-CimInstance Win32_PhysicalMemory | Select-Object Manufacturer,Capacity,ConfiguredClockSpeed);Disks=@(Get-CimInstance Win32_DiskDrive | Select-Object Model,Size,InterfaceType)} | ConvertTo-Json -Depth 4 -Compress"
        try:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                    capture_output=True, text=True, timeout=20,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)
            self.details = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            self.details = 'Detailed hardware inventory unavailable. Basic readings remain available.'

    def render(self):
        elapsed = max(0, int(time.time() - psutil.boot_time()))
        lines = ['SYSTEM', f'{platform.system()} {platform.release()} · {platform.machine()}',
                 f'Uptime: {elapsed // 86400}d {elapsed // 3600 % 24}h {elapsed // 60 % 60}m',
                 f'Logical CPU threads: {psutil.cpu_count() or "N/A"}',
                 f'Usable RAM: {psutil.virtual_memory().total / 2**30:.1f} GiB', '']
        if isinstance(self.details, dict):
            for key in ('CPU', 'GPU'):
                lines.extend([key] + [str(row.get('Name') or 'Unavailable') for row in self.details.get(key, [])] + [''])
                if key == 'CPU':
                    for row in self.details.get(key, []):
                        lines.insert(len(lines) - 1, f"{row.get('NumberOfCores') or 'N/A'} physical cores / {row.get('NumberOfLogicalProcessors') or 'N/A'} logical threads")
            lines.append('MEMORY MODULES')
            for row in self.details.get('RAM', []):
                capacity = finite(row.get('Capacity'))
                lines.append(f"{row.get('Manufacturer') or 'Unknown'} · {capacity / 2**30:.1f} GiB · configured clock {row.get('ConfiguredClockSpeed') or 'N/A'} MHz (reported)" if capacity else 'Module data unavailable')
            lines.extend(['', 'PHYSICAL DRIVES'])
            for row in self.details.get('Disks', []):
                size = finite(row.get('Size'))
                lines.append(str(row.get('Model') or 'Drive') + (f' · {size / 2**30:.1f} GiB' if size else '') + ' · ' + str(row.get('InterfaceType') or 'Interface unavailable'))
        else:
            lines.append(self.details or 'Reading detailed inventory…')
        self.text.setText('\n'.join(lines))
        self.text.setMinimumHeight(max(0, self.text.heightForWidth(max(1, self.text.width()))))

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)


class FrameSamples:
    """PresentMon frame intervals, separated by swap chain; never use monitor Hz as FPS."""
    def __init__(self):
        self.frames = deque(maxlen=4000)

    def add(self, row, now=None):
        fields = {key.strip().casefold(): value for key, value in row.items()}
        interval = finite(fields.get('msbetweenpresents'))
        # Rendered FPS counts every present, including frames the compositor drops.
        if interval is not None and 0 < interval < 10000:
            self.frames.append((time.monotonic() if now is None else now, fields.get('swapchainaddress', ''), interval))

    def intervals(self, now=None, window=2):
        now = time.monotonic() if now is None else now
        chains = {}
        for stamp, chain, interval in tuple(self.frames):
            if now - window <= stamp <= now:
                chains.setdefault(chain, []).append(interval)
        return max(chains.values(), key=len) if chains else []

    def fps(self, now=None):
        values = self.intervals(now)
        if not values:
            return None
        return 1000 * len(values) / sum(values)

    def low_fps(self, now=None):
        values = self.intervals(now, window=30)
        if len(values) < 100 or not self.intervals(now):
            return None
        slowest = sorted(values, reverse=True)[:max(1, math.ceil(len(values) * .01))]
        return 1000 * len(slowest) / sum(slowest)


class FpsReader(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.diagnostic_path = Path(owner.root) / 'pc_monitor_fps_status.json' if owner is not None and hasattr(owner, 'root') else None
        self.diagnostic_timer = QTimer(self)
        self.diagnostic_timer.timeout.connect(self.write_diagnostics)
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self.read)
        self.process.readyReadStandardError.connect(self.read_error)
        self.process.errorOccurred.connect(self.process_error)
        self.process.started.connect(lambda: setattr(self, 'started_at', time.monotonic()))
        self.process.finished.connect(self.finished)
        self.samples = FrameSamples()
        self.buffer, self.header = '', None
        self.status, self.target = 'Choose a game to capture FPS.', ''
        self.pid, self.started_at = None, None
        self.capture_clock_offset = None
        self.errors, self.stopping, self.permission_denied = '', False, False
        self.stdout_bytes, self.rows_seen, self.target_rows = 0, 0, 0
        self.stdout_prefix = ''
        self.test_active = False
        self.last_test_frame = None

    def start_test(self):
        self.stop()
        self.test_active = True
        self.last_test_frame = None
        self.pid = os.getpid()
        self.target = 'Graphics test · rendered frames'
        self.status = 'Waiting for the graphics test to render…'
        self.diagnostic_timer.start(1000)

    @Slot(float)
    def record_test_frame(self, stamp):
        if not self.test_active:
            return
        if self.last_test_frame is not None:
            self.samples.add({'msbetweenpresents': (stamp - self.last_test_frame) * 1000,
                              'swapchainaddress': 'qt-test'}, now=stamp)
        self.last_test_frame = stamp

    def write_diagnostics(self):
        if self.diagnostic_path is None:
            return
        data = dict(updated_at=time.time(), status=self.message(), pid=self.pid,
                    process_running=self.process.state() == QProcess.ProcessState.Running,
                    process_error=self.process.errorString(), permission_denied=self.permission_denied,
                    stdout_bytes=self.stdout_bytes, stdout_prefix_hex=self.stdout_prefix,
                    header=self.header, rows_seen=self.rows_seen, target_rows=self.target_rows,
                    buffered_frames=len(self.samples.frames), fps=self.samples.fps(), errors=self.errors[-4000:])
        try:
            temporary = self.diagnostic_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
            temporary.replace(self.diagnostic_path)
        except OSError:
            pass

    def start(self, executable, pid, name):
        self.stop()
        self.samples = FrameSamples()
        self.buffer, self.header = '', None
        self.target = name
        self.pid = int(pid)
        self.started_at = None
        self.capture_clock_offset = None
        self.errors, self.stopping, self.permission_denied = '', False, False
        self.stdout_bytes, self.rows_seen, self.target_rows = 0, 0, 0
        self.stdout_prefix = ''
        self.status = 'Waiting for game frames…'
        self.process.setProgram(str(executable))
        self.process.setArguments(['--process_id', str(pid), '--output_stdout', '--no_console_stats',
                                   '--v1_metrics', '--session_name', f'PCMonitor-{os.getpid()}', '--terminate_on_proc_exit'])
        self.process.start()
        self.diagnostic_timer.start(1000)
        self.write_diagnostics()

    def stop(self):
        self.test_active = False
        self.last_test_frame = None
        self.stopping = True
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
            self.process.waitForFinished(2000)
        self.samples = FrameSamples()
        self.status = 'FPS capture stopped.'
        self.started_at = None
        self.permission_denied = False
        self.diagnostic_timer.stop()
        self.write_diagnostics()

    def read(self):
        raw = bytes(self.process.readAllStandardOutput())
        self.stdout_bytes += len(raw)
        if not self.stdout_prefix and raw:
            self.stdout_prefix = raw[:96].hex()
        self.consume(raw.decode('utf-8', errors='replace'))

    def consume(self, text):
        self.buffer += text
        rows = []
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            cells = next(csv.reader([line.strip()]), [])
            normalized = [cell.strip().casefold() for cell in cells]
            if 'msbetweenpresents' in normalized and 'processid' in normalized:
                self.header = normalized
            elif self.header and len(cells) == len(self.header):
                self.rows_seen += 1
                row = dict(zip(self.header, cells))
                if row.get('processid', '').strip() != str(self.pid):
                    continue
                self.target_rows += 1
                rows.append(row)
        clocks = [finite(row.get('timeinseconds')) for row in rows]
        valid_clocks = [clock for clock in clocks if clock is not None and clock >= 0]
        if self.capture_clock_offset is None and valid_clocks:
            self.capture_clock_offset = time.monotonic() - max(valid_clocks)
        for row, seconds in zip(rows, clocks):
            stamp = self.capture_clock_offset + seconds if self.capture_clock_offset is not None and seconds is not None and seconds >= 0 else None
            self.samples.add(row, now=stamp)
        if len(self.buffer) > 65536:
            self.buffer = ''

    def read_error(self):
        message = bytes(self.process.readAllStandardError()).decode('utf-8', errors='replace').strip()
        if message:
            self.errors = (self.errors + '\n' + message)[-4000:]
            self.permission_denied = 'access denied' in self.errors.lower()
            self.status = 'Windows permission is required for FPS capture. Use Restart with Windows approval.' if self.permission_denied else message[-350:]
            self.write_diagnostics()

    def process_error(self, error):
        if not self.stopping:
            self.status = 'FPS capture could not start: ' + self.process.errorString()
            self.write_diagnostics()

    def message(self):
        fps = self.samples.fps()
        if fps is not None:
            return f'Capturing {self.target} · {fps:.1f} FPS · {1000 / fps:.2f} ms per frame'
        if self.process.state() == QProcess.ProcessState.Running and not self.permission_denied:
            if self.started_at is not None and time.monotonic() - self.started_at > 8:
                return 'No game frames yet. Select the game’s rendering process, not its launcher, or try the graphics test.'
            return 'Starting capture — waiting for rendered frames…'
        return self.status

    def finished(self, code, status):
        self.read()
        self.read_error()
        if self.stopping:
            self.status = 'FPS capture stopped.'
        elif self.permission_denied:
            self.status = 'Windows blocked FPS capture. Restart with Windows approval for this session.'
        elif code != 0:
            self.status = 'FPS capture ended with an error. ' + (self.errors.strip()[-250:] or f'PresentMon exit code {code}.')
        else:
            self.status = 'Game capture ended.'
        self.samples = FrameSamples()
        self.diagnostic_timer.stop()
        self.write_diagnostics()


def restart_with_windows_approval(owner):
    if os.name != 'nt':
        QMessageBox.information(owner, 'FPS permission', 'This FPS permission flow is for Windows.')
        return
    if ctypes.windll.shell32.IsUserAnAdmin():
        QMessageBox.information(owner, 'FPS permission', 'PC Monitor already has administrator permission. Try the graphics test or check the capture error.')
        return
    answer = QMessageBox.question(owner, 'Restart with Windows approval',
        'Restart PC Monitor as administrator for this session and open the FPS graphics test?\n\n'
        'PC Monitor and apps opened from its dock will have administrator access until closed. '
        'Windows will ask you to approve. No permanent permission or startup setting will change.',
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
    if answer != QMessageBox.StandardButton.Yes:
        return
    arguments = subprocess.list2cmdline([str(owner.root / 'pc_monitor.py'), '--fps-admin-restart', '--fps-test'])
    shell = ctypes.windll.shell32.ShellExecuteW
    shell.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                      ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    shell.restype = ctypes.c_void_p
    result = shell(None, 'runas', sys.executable, arguments, str(owner.root), 1)
    if result is None or result <= 32:
        QMessageBox.information(owner, 'Restart canceled', 'Windows approval was canceled or the restart failed. PC Monitor remains open normally.')
        return
    owner.close()
    QApplication.instance().quit()


def create_fps_test(owner):
    from PySide6.QtQuick import QQuickView, QQuickWindow, QSGRendererInterface
    from PySide6.QtQml import QQmlComponent
    if os.name == 'nt':
        QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.Direct3D11)

    class TestScene(QQuickView):
        rendered = Signal(float)

        def __init__(self):
            super().__init__()
            self.rendered.connect(owner.fps_reader.record_test_frame, Qt.ConnectionType.QueuedConnection)
            self.frameSwapped.connect(lambda: self.rendered.emit(time.monotonic()), Qt.ConnectionType.DirectConnection)
            self.setTitle('PC Monitor · Direct3D FPS test — close to stop')
            self.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
            self.resize(640, 400)
            component = QQmlComponent(self.engine(), self)
            component.setData(b'''
import QtQuick
Rectangle {
    width: 640; height: 400; color: "#06131c"
    Rectangle {
        anchors.centerIn: parent
        width: 165; height: 165; radius: 20
        color: "#35d9ee"
        Rectangle { anchors.centerIn: parent; width: 115; height: 115; radius: 12; color: "#bb91ff" }
        RotationAnimator on rotation { from: 0; to: 360; duration: 3000; loops: Animation.Infinite; running: true }
    }
    Text {
        anchors.horizontalCenter: parent.horizontalCenter; y: 20
        text: "PC MONITOR // DIRECT3D FPS TEST"
        color: "#d9f6ff"; font.pixelSize: 18
    }
    Text {
        anchors.horizontalCenter: parent.horizontalCenter; anchors.bottom: parent.bottom; anchors.bottomMargin: 18
        text: "FPS measures this test's rendered frames. Close this window to stop."
        color: "#9cc6d4"; font.pixelSize: 13
    }
}
''', QUrl())
            root = component.create()
            if root is None:
                raise RuntimeError('; '.join(error.toString() for error in component.errors()))
            self.setContent(QUrl(), component, root)

        def closeEvent(self, event):
            if owner.fps_reader.pid == os.getpid():
                owner.fps_reader.stop()
            event.accept()

    try:
        scene = TestScene()
    except RuntimeError as error:
        owner.fps_reader.status = 'Graphics test could not start: ' + str(error)
        owner.fps_reader.write_diagnostics()
        return None
    scene.show()
    return scene


class GamingOverlay(QWidget):
    def __init__(self, owner):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.owner, self.drag = owner, None
        self.setWindowTitle('PC Monitor · Gaming overlay')
        self.setStyleSheet('QWidget {background: #07151e; color: #d9f6ff;} QLabel {font-family: Consolas; font-size: 13px;} QPushButton {color: #70efff; border: 0; padding: 3px;}')
        self.setWindowOpacity(max(.35, min(1, owner.options.get('opacity', 90) / 100)))
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.title = QLabel('PC // OVERLAY · drag background')
        top.addWidget(self.title)
        close = QPushButton('×')
        close.clicked.connect(self.close)
        top.addWidget(close)
        layout.addLayout(top)
        self.readings = QLabel()
        self.readings.setTextFormat(Qt.TextFormat.PlainText)
        self.readings.setWordWrap(True)
        layout.addWidget(self.readings)
        self.frame_graph = owner.network_graph.__class__(('Mean', 'Peak'), 'Frame time · ms · last 2s', ceiling=None)
        self.frame_graph.setFixedHeight(140)
        self.frame_graph.setToolTip('Each sample shows mean and longest rendered frame interval in the last 2 seconds. Lower is smoother.')
        layout.addWidget(self.frame_graph)
        self.resize(390, 320)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self.refresh()

    def refresh(self):
        values = self.owner.current_readings
        sensors = self.owner.current_sensor_data
        def pct(key):
            value = finite(values.get(key))
            return 'N/A' if value is None else f'{value:.1f}%'
        def temp(key):
            value = finite(sensors.get(key))
            return 'N/A' if value is None else f'{value * 1.8 + 32:.1f}°F'
        fps = self.owner.fps_reader.samples.fps()
        low = self.owner.fps_reader.samples.low_fps()
        intervals = self.owner.fps_reader.samples.intervals()
        self.frame_graph.add_sample((sum(intervals) / len(intervals), max(intervals)) if intervals else (None, None))
        self.readings.setText(f'CPU {pct("cpu")}  {temp("cpu_temp")}\nGPU {pct("gpu")}  {temp("gpu_temp")}\nRAM {pct("ram")}\nFPS ' + (f'N/A · {self.owner.fps_reader.message()}' if fps is None else f'{fps:.1f} · {1000 / fps:.1f} ms\n{self.owner.fps_reader.target}'))
        self.readings.setText(self.readings.text() + '\n1% low ' + (f'{low:.1f} FPS' if low is not None else '— collecting frames') + ' · recent 30s / up to 4,000 frames')
        self.readings.setToolTip('1% low = 1000 divided by the mean duration of the slowest 1% of recent frames. Requires 100 frames; resets when capture stops.')

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self.drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag)

    def mouseReleaseEvent(self, event):
        self.drag = None

    def closeEvent(self, event):
        self.timer.stop()
        self.owner.fps_reader.stop()
        super().closeEvent(event)


class OverlaySetup(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle('PC Monitor · Gaming overlay')
        self.setMinimumWidth(500)
        self.setStyleSheet('QDialog {background: #07151e;} QLabel {color: #d9f6ff; font-size: 13px;} QPushButton,QComboBox {color: #d9f6ff; background: #0a2430; padding: 7px; font-size: 13px;}')
        layout = QVBoxLayout(self)
        note = QLabel('Overlay works over windowed/borderless games. Exclusive fullscreen may hide it. FPS uses the selected process through PresentMon and may require starting PC Monitor with Windows administrator approval. Closing the overlay stops FPS capture.')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.games = QComboBox()
        self.games.setEditable(True)
        self.games.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.games.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.games.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        layout.addWidget(self.games)
        refresh = QPushButton('Refresh running apps')
        refresh.clicked.connect(self.refresh_games)
        layout.addWidget(refresh)
        self.refresh_games()
        self.state = QLabel()
        self.state.setWordWrap(True)
        self.state.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.state)
        for text, callback in [('Show overlay', owner.show_overlay), ('Start selected game FPS', self.start_capture),
                               ('Run graphics FPS test (no game needed)', owner.start_fps_test),
                               ('Restart with Windows approval', lambda: restart_with_windows_approval(owner)),
                               ('Stop FPS capture', owner.fps_reader.stop), ('Choose PresentMon.exe', self.choose_provider),
                               ('Close', self.close)]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            layout.addWidget(button)
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.state.setText(owner.fps_reader.message()))
        self.timer.start(500)
        self.state.setText(owner.fps_reader.message())

    def refresh_games(self):
        old = self.games.currentData()
        self.games.clear()
        self.games.addItem('Select a game, or run the graphics test below', None)
        rows = []
        for process in psutil.process_iter(['pid', 'name']):
            try:
                if process.info['pid'] > 0:
                    rows.append((process.info['name'] or 'Process', process.info['pid']))
            except (psutil.Error, OSError):
                continue
        for name, pid in sorted(rows):
            self.games.addItem(f'{name} · PID {pid}', pid)
        if old:
            self.games.setCurrentIndex(max(0, self.games.findData(old)))

    def choose_provider(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose Intel PresentMon console', '', 'Executable (*.exe)')
        if path and Path(path).is_file():
            self.owner.options['presentmon_path'] = path
            self.owner.save_settings()

    def start_capture(self):
        path = Path(self.owner.options.get('presentmon_path') or self.owner.root / 'tools' / 'PresentMon-2.6.0-x64.exe')
        if not path.is_file():
            QMessageBox.information(self, 'PresentMon needed', 'Choose the Intel PresentMon console executable to enable real FPS capture.')
            return
        pid = self.games.currentData()
        if pid and self.games.currentText() == self.games.itemText(self.games.currentIndex()):
            if not psutil.pid_exists(pid):
                QMessageBox.information(self, 'App closed', 'That process has closed. Refresh the list and select the running game.')
                self.refresh_games()
                return
            name = self.games.currentText().split(' · PID ')[0]
            self.owner.fps_reader.start(path, pid, name)
            self.owner.show_overlay()
        else:
            QMessageBox.information(self, 'Select an app', 'Select a running game from the list, or use the graphics test if you do not have a game.')

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)


def apply_theme(owner):
    accent = ACCENTS.get(owner.options.get('accent'), ACCENTS['Cyan'])
    _, background, panel, text = theme_colors(owner)
    delta = owner.options.get('text_size', 13) - 13
    import re
    for widget in [owner, *owner.findChildren(QWidget)]:
        current = widget.styleSheet()
        if not current:
            continue
        if not hasattr(widget, '_theme_applied') or current != widget._theme_applied:
            widget._theme_base = current
        style = widget._theme_base
        for color in ('#35d9ee', '#70efff', '#51e0ef', '#7be8fa', '#9eeafa', '#a3eaf6'):
            style = style.replace(color, accent)
        if owner.options.get('accent') in THEMES:
            for color in ('#07151e', '#06131c', '#05151e', '#040b14'):
                style = style.replace(color, background)
            for color in ('#0a2430', '#0a1e28', '#0b2733'):
                style = style.replace(color, panel)
            style = style.replace('#d9f6ff', text)
        style = re.sub(r'font-size:\s*(\d+)px', lambda match: f'font-size: {max(9, int(match[1]) + delta)}px', style)
        if style != current:
            widget.setStyleSheet(style)
        widget._theme_applied = style
    for graph in owner.history_graphs().values():
        graph.accent = accent
        graph.theme_background = background
        graph.update()
    for meter in owner.meters.values():
        if hasattr(meter, 'dial'):
            meter.dial.update()
    owner.update()
