"""Generate the project's original monitor/bar icon; no third-party artwork."""
from pathlib import Path
import struct
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QColor, QPen

image = QImage(256, 256, QImage.Format.Format_ARGB32)
image.fill(Qt.GlobalColor.transparent)
painter = QPainter(image)
painter.setRenderHint(QPainter.RenderHint.Antialiasing)
painter.setBrush(QColor('#131e30'))
painter.setPen(QPen(QColor('#7ac2ff'), 10))
painter.drawRoundedRect(QRectF(17, 27, 222, 162), 24, 24)
painter.setPen(Qt.PenStyle.NoPen)
for x, height, color in [(54, 50, '#45aaff'), (105, 78, '#ffd166'), (156, 108, '#ff6577')]:
    painter.setBrush(QColor(color))
    painter.drawRoundedRect(QRectF(x, 162 - height, 33, height), 8, 8)
painter.setBrush(QColor('#7ac2ff'))
painter.drawRoundedRect(QRectF(112, 188, 32, 25), 5, 5)
painter.drawRoundedRect(QRectF(77, 212, 102, 14), 7, 7)
painter.end()
encoded = QByteArray()
buffer = QBuffer(encoded)
buffer.open(QIODevice.OpenModeFlag.WriteOnly)
image.save(buffer, 'PNG')
png = bytes(encoded)
icon = struct.pack('<HHH', 0, 1, 1) + struct.pack('<BBBBHHII', 0, 0, 0, 0, 1, 32, len(png), 22) + png
path = Path(__file__).resolve().parents[1] / 'pc_monitor.ico'
path.write_bytes(icon)
print('Created original pc_monitor.ico')
