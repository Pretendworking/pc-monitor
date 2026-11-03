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
    def test_live_sensors_keep_labels_but_not_stale_readings(self):
        known = {}
        sensor = dict(id='/nvme/0/temperature/0', device='SSD', name='Composite', kind='temperature', value=40)
        limit = dict(sensor, id='limit', name='Critical Temperature', value=85)
        fresh = m.live_sensor_lines(dict(hardware=[sensor, limit], cpu_temp=50), known)
        self.assertIn('SSD · Composite: 104.0 °F', fresh)
        self.assertFalse(any('Critical' in line for line in fresh))
        stale = m.live_sensor_lines(dict(hardware=[sensor], hardware_age=3, cpu_temp=50, cpu_temp_age=3), known)
        self.assertIn('SSD · Composite: N/A', stale)
        self.assertIn('CPU temperature: N/A', stale)
        self.assertIn('SSD · Composite: N/A', m.live_sensor_lines({}, known))

    def test_alerts_require_sustained_fresh_values_and_rearm(self):
        alerts = m.extras.SustainedAlerts()
        options = dict(m.DEFAULTS, alerts_enabled=True, alert_seconds=5)
        self.assertEqual(alerts.update({'cpu': 95}, options, now=0), [])
        self.assertEqual(alerts.update({'cpu': None}, options, now=5), [])
        self.assertEqual(alerts.update({'cpu': 95}, options, now=6), [])
        self.assertEqual(len(alerts.update({'cpu': 95}, options, now=11)), 1)
        self.assertEqual(alerts.update({'cpu': 99}, options, now=12), [])
        alerts.update({'cpu': 20}, options, now=13)
        self.assertFalse(alerts.active)
        self.assertEqual(alerts.update({'cpu': 95}, options, now=400), [])
        self.assertEqual(len(alerts.update({'cpu': 95}, options, now=405)), 1)
        alerts.update({}, dict(options, alerts_enabled=False), now=406)
        self.assertFalse(alerts.active)

    def test_network_quality_loss_zero_ping_and_jitter_gaps(self):
        result = m.extras.ping_summary([0, 10, None, 30])
        self.assertAlmostEqual(result['ping'], 40 / 3)
        self.assertEqual(result['loss'], 25)
        self.assertEqual(result['jitter'], 10)
        self.assertIsNone(m.extras.ping_summary([None] * 10)['ping'])
        self.assertEqual(m.extras.ping_summary([None] * 10)['loss'], 100)

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
    def test_floating_panels_round_trip_and_persist(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(m, 'CONFIG', Path(directory) / 'settings.json'), \
             patch.object(m, 'Sensors', FakeSensors):
            widget = m.Monitor()
            try:
                widget.float_panels()
                self.assertTrue(widget.options['separate_panels'])
                self.assertIs(widget.panels['cpu'].scroll.widget(), widget.meters['cpu'])
                widget.panels['cpu'].move(30, 40)
                widget.panels['cpu'].save_settings()
                self.assertTrue(m.load_settings()['floating_enabled'])
                self.assertEqual(m.load_settings()['panel_positions']['cpu']['x'], 30)
                widget.dock_panels()
                self.assertFalse(widget.panels)
                self.assertIs(widget.dashboard_grid.itemAtPosition(0, 0).widget(), widget.meters['cpu'])
                self.assertFalse(m.load_settings()['floating_enabled'])
            finally:
                widget.close()

    def test_network_speed_rejects_partial_download_and_can_cancel(self):
        graph = m.HistoryGraph()
        panel = m.extras.NetworkQuality(NS(network_graph=graph))
        try:
            panel.phase = 'download'
            panel.started = m.time.monotonic() - 1
            panel.set_busy(True)
            panel.reply = NS(readAll=lambda: b'partial',
                error=lambda: m.extras.QNetworkReply.NetworkError.NoError,
                errorString=lambda: '', deleteLater=lambda: None)
            panel.finish_transfer()
            self.assertIn('Incomplete download', panel.speed_result.text())
            self.assertTrue(panel.speed.isEnabled())
            aborted = []
            panel.reply = NS(abort=lambda: aborted.append(True), deleteLater=lambda: None)
            panel.set_busy(True)
            panel.cancel()
            self.assertEqual(aborted, [True])
            self.assertIsNone(panel.reply)
            self.assertTrue(panel.speed.isEnabled())
        finally:
            panel.shutdown()
            panel.close()
            graph.close()

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
                self.assertEqual([tabs.tabText(i) for i in range(tabs.count())], ['Appearance', 'Monitoring', 'Theme', 'Alerts'])
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

    def test_rendered_test_frames_measure_and_stop(self):
        reader = m.extras.FpsReader(None)
        reader.start_test()
        for stamp in (10, 10.02, 10.04):
            reader.record_test_frame(stamp)
        self.assertAlmostEqual(reader.samples.fps(now=10.04), 50)
        self.assertIsNone(reader.samples.fps(now=14))
        reader.stop()
        reader.record_test_frame(14.02)
        self.assertEqual(len(reader.samples.frames), 0)

    def test_frame_samples_separate_swap_chains_and_expire(self):
        frames = m.extras.FrameSamples()
        for stamp in (10, 10.1, 10.2):
            frames.add(dict(MsBetweenPresents='16.6666667', SwapChainAddress='main'), now=stamp)
        frames.add(dict(MsBetweenPresents='100', SwapChainAddress='secondary'), now=10.2)
        frames.add(dict(MsBetweenPresents='0', SwapChainAddress='main'), now=10.3)
        frames.add(dict(MsBetweenPresents='nan', SwapChainAddress='main'), now=10.3)
        self.assertAlmostEqual(frames.fps(now=11), 60, places=4)
        frames.add(dict(MsBetweenPresents='16.6666667', SwapChainAddress='main', Dropped='1'), now=10.3)
        self.assertEqual(len(frames.frames), 5)
        self.assertAlmostEqual(frames.fps(now=11), 60, places=4)
        self.assertIsNone(frames.fps(now=14))

    def test_one_percent_low_uses_slowest_frames_and_expires(self):
        frames = m.extras.FrameSamples()
        for index in range(100):
            frames.add({'msbetweenpresents': 50 if index == 99 else 10}, now=10 + index / 100)
        self.assertAlmostEqual(frames.low_fps(now=11), 20)
        self.assertIsNone(frames.low_fps(now=40))

    def test_dashboard_customization_and_overlay_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(m, 'CONFIG', Path(directory) / 'settings.json'), \
             patch.object(m, 'Sensors', FakeSensors):
            widget = m.Monitor()
            widget.show()
            try:
                widget.move_monitor('gpu', 'cpu')
                self.assertIs(widget.dashboard_grid.itemAtPosition(0, 0).widget(), widget.meters['gpu'])
                widget.options.update(accent='Violet', text_size=15, card_heights={'cpu': 175})
                widget.apply_settings()
                self.assertEqual(widget.meters['cpu'].graph.height(), 175)
                widget.options['card_spans']['gpu'] = 2
                widget.apply_order()
                self.assertIs(widget.dashboard_grid.itemAtPosition(0, 1).widget(), widget.meters['gpu'])
                self.assertIs(widget.dashboard_grid.itemAtPosition(1, 0).widget(), widget.meters['cpu'])
                style = widget.inspect_button.styleSheet()
                m.extras.apply_theme(widget)
                self.assertEqual(widget.inspect_button.styleSheet(), style)
                self.assertIn('#bb91ff', style)
                self.assertIn('font-size: 14px', style)
                widget.show_overlay()
                overlay = widget.overlay_window
                self.assertIn('FPS N/A', overlay.readings.text())
                overlay.close()
                widget.show_overlay()
                self.assertIsNot(widget.overlay_window, overlay)
                widget.save_settings()
                saved = m.load_settings()
                self.assertEqual(saved['dashboard_order'][0], 'gpu')
                self.assertEqual(saved['card_heights']['cpu'], 175)
                self.assertEqual(saved['card_spans']['gpu'], 2)
                self.assertEqual(saved['accent'], 'Violet')
                widget.meters['cpu'].dial.grab()
            finally:
                widget.close()

    def test_presentmon_command_targets_one_process_and_stops(self):
        reader = m.extras.FpsReader(None)
        with patch.object(reader.process, 'start'):
            reader.start('PresentMon.exe', 1234, 'game.exe')
        args = reader.process.arguments()
        self.assertEqual(args[args.index('--process_id') + 1], '1234')
        self.assertIn('--v1_metrics', args)
        self.assertNotIn('--restart_as_admin', args)
        reader.stop()
        self.assertIsNone(reader.samples.fps())

    def test_presentmon_csv_handles_case_chunks_and_target_pid(self):
        reader = m.extras.FpsReader(None)
        reader.pid = 1234
        with patch.object(m.extras.time, 'monotonic', return_value=10):
            reader.consume('Application,ProcessID,SwapChainAddress,TimeInSeconds,msBetween')
            reader.consume('Presents,Dropped\nother.exe,4321,0x1,1,1,0\ngame.exe,1234,0x1,1,16.6666667,0\n')
            reader.consume('game.exe,1234,0x1,2,16.6666667,0\n')
        self.assertEqual(len(reader.samples.frames), 2)
        self.assertAlmostEqual(reader.samples.fps(now=10), 60, places=4)
        self.assertIsNone(reader.samples.fps(now=14))
        reader.stop()

    def test_presentmon_permission_error_is_actionable_and_stop_clears_it(self):
        reader = m.extras.FpsReader(None)
        with patch.object(reader.process, 'readAllStandardError', return_value=b'error: failed to start trace session: access denied.'):
            reader.read_error()
            reader.finished(1, m.extras.QProcess.ExitStatus.NormalExit)
        self.assertTrue(reader.permission_denied)
        self.assertIn('Restart with Windows approval', reader.message())
        reader.stop()
        self.assertFalse(reader.permission_denied)
        self.assertEqual(reader.message(), 'FPS capture stopped.')

    def test_fps_diagnostics_are_local_and_include_parser_state(self):
        with tempfile.TemporaryDirectory() as directory:
            holder = m.QWidget()
            holder.root = Path(directory)
            reader = m.extras.FpsReader(holder)
            reader.pid = 123
            reader.consume('Application,ProcessID,msBetweenPresents\ngame.exe,123,16.7\n')
            reader.write_diagnostics()
            report = json.loads((Path(directory) / 'pc_monitor_fps_status.json').read_text(encoding='utf-8'))
            self.assertEqual(report['target_rows'], 1)
            self.assertEqual(report['header'], ['application', 'processid', 'msbetweenpresents'])
            self.assertFalse(report['process_running'])
            self.assertEqual(report['buffered_frames'], 1)
            reader.stop()
            holder.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows approval flow')
    def test_declining_fps_restart_keeps_normal_app_open(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(m, 'CONFIG', Path(directory) / 'settings.json'), \
             patch.object(m, 'Sensors', FakeSensors):
            widget = m.Monitor()
            widget.show()
            try:
                with patch.object(m.extras.ctypes.windll.shell32, 'IsUserAnAdmin', return_value=0), \
                     patch.object(m.QMessageBox, 'question', return_value=m.QMessageBox.StandardButton.No), \
                     patch.object(m.extras.ctypes.windll.shell32, 'ShellExecuteW') as execute:
                    m.extras.restart_with_windows_approval(widget)
                execute.assert_not_called()
                self.assertTrue(widget.isVisible())
            finally:
                widget.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows media controls')
    def test_media_buttons_send_only_expected_media_keys(self):
        with patch.object(m.extras.ctypes.windll.user32, 'keybd_event') as send:
            m.extras.media_key('mute')
            self.assertEqual(send.call_args_list[0].args, (0xAD, 0, 0, 0))
            self.assertEqual(send.call_args_list[1].args, (0xAD, 0, 2, 0))
        with self.assertRaises(OSError):
            m.extras.media_key('unsupported')

    def test_customization_migration_validates_saved_values(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(m, 'CONFIG', Path(directory) / 'settings.json'):
            m.CONFIG.write_text(json.dumps(dict(accent='invalid', glow=999, text_size=900,
                dashboard_order=['gpu', 'gpu', 'invalid'], card_spans={'cpu': 2, 'gpu': -1},
                card_heights={'cpu': 900, 'ram': 'invalid'}, favorite_apps=['app.exe', 42, 'note.txt'])), encoding='utf-8')
            saved = m.load_settings()
            self.assertEqual(saved['accent'], 'Cyan')
            self.assertEqual(saved['glow'], 100)
            self.assertEqual(saved['text_size'], 16)
            self.assertEqual(saved['dashboard_order'], ['gpu', 'cpu', 'ram', 'disk'])
            self.assertEqual(saved['card_spans'], {'cpu': 2})
            self.assertEqual(saved['card_heights'], {'cpu': 300})
            self.assertEqual(saved['favorite_apps'], ['app.exe'])


if __name__ == '__main__':
    unittest.main()
