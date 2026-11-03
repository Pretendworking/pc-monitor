import importlib.util
import csv
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

    def test_history_preserves_gaps_and_bounds_memory(self):
        graph = m.HistoryGraph()
        graph.add_sample((10,), timestamp=100)
        graph.add_sample((None,), timestamp=101)
        graph.add_sample((30,), timestamp=102)
        graph.add_sample((40,), timestamp=130)
        self.assertEqual(graph.segments(0, 130), [[(100, 10)], [(102, 30)], [(130, 40)]])
        graph.add_sample((float('nan'),), timestamp=131)
        self.assertIsNone(graph.samples[-1][1][0])
        graph.samples.clear()
        for stamp in range(4000):
            graph.add_sample((stamp % 100,), timestamp=stamp)
        self.assertLessEqual(len(graph.samples), 3601)
        self.assertEqual(graph.samples[0][0], 399)
        graph.range_selector.setCurrentIndex(2)
        self.assertEqual(graph.window, 3600)
        self.assertEqual(graph.sample_at(1000), (1000, (0,)))
        graph.range_selector.setCurrentIndex(0)
        self.assertTrue(all(t >= 3699 for segment in graph.segments(0, 3999) for t, _ in segment))
        self.assertIsNone(graph.sample_at(0))
        graph.close()

    def test_resource_report_updates_and_reopens_cleanly(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(m, 'CONFIG', Path(directory) / 'settings.json'), \
             patch.object(m, 'Sensors', FakeSensors):
            widget = m.Monitor()
            try:
                widget.current_readings = dict(cpu=95, ram=90, disk=50, gpu=None)
                widget.current_sensor_data = dict(top_cpu=[dict(name='<Example>', cpu=45)],
                                                  top_ram=[dict(name='Example', ram=2**30)])
                widget.open_diagnostics()
                dialog = widget.diagnostic_dialog
                text = '\n'.join(label.text() for label in dialog.findChildren(m.QLabel))
                self.assertIn('Memory use is high', text)
                self.assertIn('<Example> — 45.0%', text)
                self.assertIn('GPU N/A', text)
                widget.open_diagnostics()
                self.assertIs(widget.diagnostic_dialog, dialog)
                dialog.close()
                self.assertIsNone(widget.diagnostic_dialog)
                widget.open_diagnostics()
                widget.diagnostic_dialog.close()
            finally:
                widget.close()

    def test_pressure_report_distinguishes_missing_normal_and_high(self):
        self.assertIn('Waiting', m.pressure_summary({}))
        self.assertIn('No high', m.pressure_summary(dict(cpu=20, ram=50, disk=60, gpu=None)))
        self.assertIn('nearly full', m.pressure_summary(dict(disk=95)))
        self.assertNotIn('CPU is busy', m.pressure_summary(dict(cpu=float('nan'))))

    def test_expand_history_persists_and_refreshes_while_collapsed(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(m, 'CONFIG', Path(directory) / 'settings.json'), \
             patch.object(m, 'Sensors', FakeSensors):
            m.CONFIG.write_text(json.dumps(dict(m.DEFAULTS, dashboard=False)), encoding='utf-8')
            widget = m.Monitor()
            widget.show()
            try:
                meter = widget.meters['cpu']
                self.assertTrue(meter.graph.isHidden())
                meter.toggle.click()
                self.app.processEvents()
                self.assertFalse(meter.graph.isHidden())
                self.assertTrue(m.load_settings()['history_cpu'])
                meter.graph.grab()  # Exercise real Qt painting.
                meter.toggle.click()
                count = len(meter.graph.samples)
                widget.refresh()
                self.assertGreater(len(meter.graph.samples), count)
                self.assertTrue(meter.graph.isHidden())
                widget.network_graph.grab()
            finally:
                widget.close()

    def test_settings_migration_reorder_and_visibility(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(m, 'CONFIG', Path(directory) / 'settings.json'), \
             patch.object(m, 'Sensors', FakeSensors):
            m.CONFIG.write_text(json.dumps(dict(m.DEFAULTS, separate_panels=True, dashboard=False)), encoding='utf-8')
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

    def test_dashboard_switch_preserves_history_and_visibility(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(m, 'CONFIG', Path(directory) / 'settings.json'), \
             patch.object(m, 'Sensors', FakeSensors):
            widget = m.Monitor()
            widget.show()
            self.app.processEvents()
            try:
                self.assertTrue(widget.options['dashboard'])
                self.assertIs(widget.dashboard_grid.itemAtPosition(0, 0).widget(), widget.meters['cpu'])
                self.assertIs(widget.dashboard_grid.itemAtPosition(0, 1).widget(), widget.meters['gpu'])
                self.assertFalse(widget.meters['cpu'].graph.isHidden())
                count = len(widget.meters['cpu'].graph.samples)
                widget.toggle_dashboard()
                self.assertFalse(widget.options['dashboard'])
                self.assertIs(widget.content_layout.itemAt(0).widget(), widget.meters['cpu'])
                self.assertEqual(len(widget.meters['cpu'].graph.samples), count)
                widget.options['show_gpu'] = False
                widget.toggle_dashboard()
                self.assertTrue(widget.meters['gpu'].isHidden())
                self.assertTrue(m.load_settings()['dashboard'])
                widget.grab()
            finally:
                widget.close()

    def test_shared_range_mixed_state_and_csv_export(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(m, 'CONFIG', Path(directory) / 'settings.json'), \
             patch.object(m, 'Sensors', FakeSensors):
            widget = m.Monitor()
            try:
                widget.global_range.setCurrentIndex(2)
                self.assertTrue(all(graph.window == 3600 for graph in widget.history_graphs().values()))
                widget.meters['cpu'].graph.range_selector.setCurrentIndex(0)
                self.assertEqual(widget.global_range.currentIndex(), -1)
                widget.global_range.setCurrentIndex(0)
                self.assertTrue(all(graph.window == 300 for graph in widget.history_graphs().values()))
                now = m.time.monotonic()
                graph = widget.meters['cpu'].graph
                graph.add_sample((None,), timestamp=now)
                graph.add_sample((12.5,), timestamp=now)
                output = Path(directory) / 'readings.csv'
                with patch.object(m.QFileDialog, 'getSaveFileName', return_value=(str(output), 'CSV')), \
                     patch.object(m.QMessageBox, 'information'):
                    widget.export_history()
                with output.open(encoding='utf-8-sig', newline='') as saved:
                    rows = list(csv.DictReader(saved))
                cpu = [row for row in rows if row['metric'] == 'cpu']
                self.assertTrue(any(row['value'] == '' and row['status'] == 'unavailable' for row in cpu))
                self.assertTrue(any(row['value'] == '12.5' for row in cpu))
                self.assertTrue(all(row['timestamp_utc'].endswith('+00:00') for row in rows))
                self.assertTrue(widget.options['dashboard'])
            finally:
                widget.close()

    def test_sensor_status_and_export_window(self):
        self.assertEqual(m.sensor_status({}), 'SENSORS UNAVAILABLE')
        self.assertEqual(m.sensor_status(dict(hardware=[{}], hardware_age=1)), 'SENSORS LIVE')
        self.assertEqual(m.sensor_status(dict(hardware=[{}], hardware_age=4)), 'SENSORS DELAYED')
        graph = m.HistoryGraph()
        graph.samples.extend([(1, (5,)), (4999, (None,)), (5000, (30,)), (5001, (40,))])
        rows = m.history_rows({'cpu': graph}, now=5000)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][-1], 'unavailable')
        self.assertEqual(rows[1][3], 30)
        graph.close()


if __name__ == '__main__':
    unittest.main()
