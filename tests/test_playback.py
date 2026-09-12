import asyncio
import unittest
from unittest import mock

from omarchy_voice.playback import LiveSpeaker


class BufferTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.speaker = LiveSpeaker(24000)
        self.speaker._pump = mock.Mock(done=lambda: False)
        self.addAsyncCleanup(self.cleanup)

    async def cleanup(self):
        self.speaker._pump = None
        await self.speaker.close()

    async def test_jitter_does_not_insert_gaps_or_lose_samples(self):
        # 100 ms network packets, one delayed by 80 ms. Playback begins after
        # 120 ms; compare every output sample with the source after that delay.
        source = (b'\x00\x20' * 2400) * 10
        output = b''
        with mock.patch('omarchy_voice.playback.time.monotonic') as clock:
            for tick in range(60):
                now = tick * .02
                clock.return_value = now
                for packet in range(10):
                    arrival = packet * 5 + (4 if packet == 4 else 0)
                    if tick == arrival:
                        await self.speaker.write(source[packet * 4800:(packet + 1) * 4800])
                output += self.speaker._next_frame(now)
        first = output.index(b'\x00\x20')
        self.assertLessEqual(first / 48000, .14)
        self.assertEqual(output[first:first + len(source)], source)
        self.assertEqual(self.speaker._stats['played_samples'], len(source) // 2)

    async def test_packet_just_after_tick_does_not_insert_silence(self):
        self.speaker._primed = True
        waiting = asyncio.create_task(self.speaker._wait_frame())
        await asyncio.sleep(.001)
        chunk = b'\x00\x20' * 480
        await self.speaker.write(chunk)
        await waiting
        self.assertEqual(self.speaker._next_frame(__import__('time').monotonic()), chunk)
        self.assertEqual(self.speaker._stats['underruns'], 0)

    async def test_short_last_chunk_drains_without_waiting_for_more_data(self):
        with mock.patch('omarchy_voice.playback.time.monotonic', return_value=0):
            await self.speaker.write(b'\x00\x20' * 240)
        self.assertEqual(self.speaker._next_frame(.10), bytes(960))
        self.assertEqual(self.speaker._next_frame(.13)[:480], b'\x00\x20' * 240)

    async def test_overflow_fails_instead_of_silently_dropping_words(self):
        with self.assertRaisesRegex(RuntimeError, 'queue exceeded'):
            await self.speaker.write(bytes(480001 * 2))
        self.assertEqual(self.speaker._pcm, b'')

    async def test_incomplete_pcm_rejected(self):
        with self.assertRaises(ValueError):
            await self.speaker.write(b'1')

    async def test_interrupt_clears_buffer_and_meter(self):
        await self.speaker.write(b'\x00\x20' * 4800)
        self.speaker._pump = None
        await self.speaker.interrupt()
        self.assertEqual(self.speaker._pcm, b'')
        self.assertEqual(self.speaker.level_now(), 0)
        self.assertFalse(self.speaker.is_playing())

    async def test_playback_failure_is_reported_to_the_session(self):
        self.speaker._error = 'device disconnected'
        with self.assertRaisesRegex(RuntimeError, 'device disconnected'):
            self.speaker.check_error()


class PumpTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_process_for_concurrent_chunks_and_paced_playback(self):
        proc = mock.Mock(returncode=None)
        proc.stdin.drain = mock.AsyncMock()
        proc.stderr.readline = mock.AsyncMock(side_effect=asyncio.CancelledError)
        proc.wait = mock.AsyncMock(return_value=0)
        speaker = LiveSpeaker(24000)
        with mock.patch('omarchy_voice.playback.asyncio.create_subprocess_exec',
                        new=mock.AsyncMock(return_value=proc)) as spawn:
            await asyncio.gather(*(speaker.write(b'\x00\x20' * 2400) for _ in range(3)))
            await asyncio.sleep(.10)
            self.assertEqual(spawn.await_count, 1)
            self.assertGreater(proc.stdin.write.call_count, 2)
            self.assertLess(proc.stdin.write.call_count, 9)
            await speaker.close()
            self.assertEqual(speaker._pcm, b'')


class QuietReserveTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_delivery_and_jitter_preserve_every_speech_burst(self):
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / 'tools/bench_playback_reserve.py'
        spec = importlib.util.spec_from_file_location('reserve_bench', path)
        bench = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bench)
        result = await bench.measure(LiveSpeaker, 'test')
        for burst in result['bursts']:
            self.assertTrue(burst['samples_preserved'])
            self.assertEqual(burst['internal_gap_ms'], 0)
            self.assertLessEqual(burst['onset_after_first_packet_ms'], 160)

    async def test_quiet_tail_drains_when_source_stops(self):
        speaker = LiveSpeaker(24000)
        speaker._pump = mock.Mock(done=lambda: False)
        with mock.patch('omarchy_voice.playback.time.monotonic', return_value=0):
            await speaker.write(bytes(4800))
        for tick in range(20):
            self.assertFalse(any(speaker._next_frame(tick * .02)))
        self.assertEqual(speaker._stats['played_samples'], 2400)
        self.assertEqual(speaker._pcm, b'')
        speaker._pump = None
        await speaker.close()
