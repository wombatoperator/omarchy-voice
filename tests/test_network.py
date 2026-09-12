import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from omarchy_voice.config import Config, load
from omarchy_voice.network import SocketMonitor, monitored_socket


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    def monitor(self):
        monitor = SocketMonitor(Config(), mock.Mock(), 'live')
        monitor.emit = mock.AsyncMock()
        return monitor

    async def test_ping_waits_for_matching_pong(self):
        monitor = self.monitor()
        pong = asyncio.get_running_loop().create_future()
        ws = mock.Mock(ping=mock.AsyncMock(return_value=pong), transport=None)
        asyncio.get_running_loop().call_later(.02, pong.set_result, None)
        await monitor.probe(ws)
        event = monitor.emit.call_args.kwargs
        self.assertGreaterEqual(event['rtt_ms'], 15)
        self.assertEqual(event['successes'], 1)
        self.assertEqual(event['status'], 'ok')

    async def test_timeout_is_not_a_fake_zero_latency_or_socket_close(self):
        monitor = self.monitor()
        monitor.timeout = .01
        ws = mock.Mock(ping=mock.AsyncMock(return_value=asyncio.get_running_loop().create_future()), transport=None)
        await monitor.probe(ws)
        event = monitor.emit.call_args.kwargs
        self.assertEqual(event['status'], 'timeout')
        self.assertIsNone(event['rtt_ms'])
        self.assertIsNone(event['rtt_p50_ms'])
        ws.close.assert_not_called()

    async def test_loop_stall_is_measured_separately(self):
        monitor = self.monitor()
        task = asyncio.create_task(monitor.lag_loop())
        await asyncio.sleep(.02)
        time.sleep(.14)
        await asyncio.sleep(.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertGreater(monitor.max_loop_lag_ms, 40)

    async def test_disabled_monitor_does_not_ping_or_log(self):
        ws = mock.Mock()
        @asynccontextmanager
        async def context():
            yield ws
        with mock.patch('omarchy_voice.network.SocketMonitor') as cls:
            async with monitored_socket(context(), Config(network_enabled=False), mock.Mock(), 'live') as result:
                self.assertIs(result, ws)
            cls.assert_not_called()

    async def test_logging_failure_does_not_propagate(self):
        monitor = self.monitor()
        monitor.file.write = mock.Mock(side_effect=OSError('disk full'))
        monitor.feedback.log.side_effect = OSError('disk full')
        await SocketMonitor.emit(monitor, 'network_sample', rtt_ms=10)

    async def test_real_websocket_logs_and_cleans_up(self):
        from websockets.asyncio.server import serve
        from websockets.asyncio.client import connect
        async def echo(ws):
            async for message in ws:
                await ws.send(message)
        with tempfile.TemporaryDirectory() as tmp, mock.patch('omarchy_voice.network.STATE_DIR', Path(tmp)):
            async with serve(echo, '127.0.0.1', 0) as server:
                port = server.sockets[0].getsockname()[1]
                async with monitored_socket(connect(f'ws://127.0.0.1:{port}'), Config(), mock.Mock(), 'realtime',
                                            endpoint=f'ws://127.0.0.1:{port}') as ws:
                    await ws.send('hello')
                    self.assertEqual(await ws.recv(), 'hello')
                    await asyncio.sleep(.08)
            records = [json.loads(x) for x in (Path(tmp)/'network-trace.jsonl').read_text().splitlines()]
            self.assertEqual([x['event'] for x in records], ['network_connected', 'network_sample', 'network_closed'])
            self.assertEqual(records[-1]['successes'], 1)
            self.assertEqual(records[-1]['endpoint_host'], '127.0.0.1')
            self.assertEqual(records[-1]['close_code'], 1000)
            self.assertEqual(len({x['connection_id'] for x in records}), 1)
            self.assertEqual((Path(tmp)/'network-trace.jsonl').stat().st_mode & 0o777, 0o600)

    async def test_connect_failure_logged_without_credentials(self):
        @asynccontextmanager
        async def context():
            raise OSError('secret-url')
            yield
        with tempfile.TemporaryDirectory() as tmp, mock.patch('omarchy_voice.network.STATE_DIR', Path(tmp)):
            with self.assertRaises(OSError):
                async with monitored_socket(context(), Config(), mock.Mock(), 'live'):
                    pass
            data = (Path(tmp)/'network-trace.jsonl').read_text()
            self.assertIn('network_connection_error', data)
            self.assertNotIn('secret-url', data)

    async def test_sample_window_and_invalid_config_are_bounded(self):
        monitor = SocketMonitor(Config(network_interval_seconds=0, network_timeout_seconds=float('nan')), mock.Mock(), 'live')
        self.assertEqual(monitor.interval, 1)
        self.assertEqual(monitor.timeout, 5)
        monitor.samples.extend(range(100))
        self.assertEqual(monitor.summary()['window_samples'], 30)
        self.assertEqual(monitor.summary()['rtt_p95_ms'], 98)

    async def test_config_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'config.toml'
            path.write_text('[network]\nenabled=false\ninterval_seconds=10\ntimeout_seconds=3\n')
            config = load(path)
            self.assertFalse(config.network_enabled)
            self.assertEqual(config.network_interval_seconds, 10)
            self.assertEqual(config.unknown_keys, [])
