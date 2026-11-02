import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('monitor', ROOT / 'pc_monitor.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Readings(unittest.TestCase):
    def test_thresholds_and_units(self):
        self.assertEqual([m.color(x) for x in (49.9, 50, 79.9, 80)],
                         ['#45aaff', '#ffd166', '#ffd166', '#ff6577'])
        self.assertEqual(m.fahrenheit(51), '123.8 °F')
        self.assertEqual(m.network_rate(1_250_000), '10.0 Mbps')
        self.assertEqual(m.network_rate(1_000_000, 'MB/s'), '1.0 MB/s')
        self.assertIsNone(m.counter_rate(5, 100, 1))

    def test_gpu_uses_busy_engine_and_keeps_sensor_name(self):
        records = [
            dict(Identifier='/gpu-amd/0/load/0', Name='GPU Core', Value=0),
            dict(Identifier='/gpu-amd/0/load/1', Name='D3D 3D', Value=37.5),
            dict(Identifier='/gpu-amd/0/load/2', Name='GPU Memory', Value=99),
            dict(Identifier='/gpu-amd/0/temperature/0', Name='GPU VR SoC', Value=51),
            dict(Identifier='/lpc/0/fan/0', Name='Fan', Value=1200),
        ]
        data = m.parse_sensor_records(records)
        self.assertEqual(data['gpu'], 37.5)
        self.assertEqual(data['gpu_engine_name'], 'D3D 3D')
        self.assertEqual(data['gpu_temp_name'], 'GPU VR SoC')
        self.assertEqual(len(data['hardware']), 5)

    def test_sensor_gaps_expire(self):
        cache, _ = m.merge_sensor_samples({}, {'gpu_temp': 51, 'gpu_temp_name': 'GPU VR SoC'}, 100)
        cache, data = m.merge_sensor_samples(cache, {}, 103)
        self.assertEqual(data['gpu_temp'], 51)
        _, data = m.merge_sensor_samples(cache, {}, 106)
        self.assertNotIn('gpu_temp', data)

    def test_network_excludes_loopback_and_uses_elapsed_time(self):
        now = [0.0]
        stats = {name: NS(isup=True) for name in ('Wi-Fi', 'Loopback Pseudo-Interface 1')}
        counters = {'Wi-Fi': NS(bytes_recv=100, bytes_sent=0),
                    'Loopback Pseudo-Interface 1': NS(bytes_recv=10**12, bytes_sent=0)}
        sampler = m.NetworkSampler()
        with patch.object(m.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(m.psutil, 'net_if_stats', return_value=stats), \
             patch.object(m.psutil, 'net_io_counters', side_effect=lambda **kw: dict(counters)):
            self.assertIsNone(sampler.sample()['download'])
            now[0] = 2
            counters['Wi-Fi'] = NS(bytes_recv=2_500_100, bytes_sent=500_000)
            data = sampler.sample()
            self.assertEqual(data['adapter'], 'Wi-Fi')
            self.assertEqual(data['download'], 1_250_000)
            self.assertEqual(data['upload'], 250_000)
            now[0] = 2.1
            self.assertEqual(sampler.sample(), data)
            now[0] = 4
            counters['Wi-Fi'] = NS(bytes_recv=5, bytes_sent=0)
            self.assertIsNone(sampler.sample()['download'])

    def test_process_percent_and_pid_reuse(self):
        def proc(born, cpu):
            return NS(info=dict(pid=7, create_time=born, name='Example',
                                cpu_times=NS(user=cpu, system=0), memory_info=NS(rss=1024)))
        sampler = m.ProcessSampler()
        with patch.object(m.psutil, 'cpu_count', return_value=4), \
             patch.object(m.psutil, 'process_iter', side_effect=[[proc(1, 10)], [proc(1, 12)], [proc(2, 100)]]), \
             patch.object(m.time, 'monotonic', side_effect=[100, 102, 104]):
            self.assertEqual(sampler.sample()['top_cpu'], [])
            self.assertEqual(sampler.sample()['top_cpu'][0]['cpu'], 25)
            self.assertEqual(sampler.sample()['top_cpu'], [])


class FakeSensors:
    def __init__(self):
        self.stop = threading.Event()
    def snapshot(self):
        return {}


class Widget(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = m.QApplication.instance() or m.QApplication([])

    def test_settings_migration_reorder_and_visibility(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(m, 'CONFIG', Path(directory) / 'settings.json'), \
             patch.object(m, 'Sensors', FakeSensors):
            m.CONFIG.write_text(json.dumps(dict(m.DEFAULTS, separate_panels=True)), encoding='utf-8')
            widget = m.Monitor()
            widget.show()
            self.app.processEvents()
            try:
                self.assertFalse(widget.options['separate_panels'])
                widget.move_monitor('gpu', 'cpu')
                self.assertIs(widget.content_layout.itemAt(0).widget(), widget.meters['gpu'])
                self.assertEqual(m.load_settings()['monitor_order'][0], 'gpu')
                widget.options['show_gpu'] = False
                widget.apply_settings()
                self.assertTrue(widget.meters['gpu'].isHidden())
                dialog = m.SettingsDialog(widget)
                tabs = dialog.findChild(m.QTabWidget)
                self.assertEqual([tabs.tabText(i) for i in range(tabs.count())], ['Appearance', 'Monitoring'])
                dialog.close()
            finally:
                widget.close()


if __name__ == '__main__':
    unittest.main()
