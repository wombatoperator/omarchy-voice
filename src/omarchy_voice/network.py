"""Diagnostics on the existing voice socket; never opens a paid session.

Ping/Pong measures transport round trips, not model inference. Local scheduling
and socket buffering can inflate it, so record those alongside each sample.
"""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
import json
import math
import statistics
import time
import uuid
from urllib.parse import urlsplit

from .config import STATE_DIR
from .trace import Trace


class SocketMonitor:
    def __init__(self, config, feedback, engine, trace=None, endpoint=""):
        self.config, self.feedback, self.engine = config, feedback, engine
        def bounded(value, default, low, high):
            return min(high, max(low, value)) if type(value) in (int, float) and math.isfinite(value) else default
        self.interval = bounded(config.network_interval_seconds, 20, 1, 300)
        self.timeout = bounded(config.network_timeout_seconds, 5, .1, 30)
        self.trace = trace
        self.file = Trace(STATE_DIR / "network-trace.jsonl")
        self.connection_id = uuid.uuid4().hex
        self.endpoint_host = urlsplit(endpoint).hostname
        self.samples = deque(maxlen=30)
        self.successes = self.timeouts = 0
        self.loop_lag_ms = self.max_loop_lag_ms = 0.0

    async def emit(self, event, **data):
        record = dict(engine=self.engine, connection_id=self.connection_id,
                      endpoint_host=self.endpoint_host, **data)
        def write():
            # Logging failure must never end a voice conversation.
            for sink in (lambda: self.file.write(event, **record),
                         lambda: self.feedback.log("network " + json.dumps(dict(event=event, **record))),
                         lambda: self.trace(event, **record) if self.trace else None):
                try:
                    sink()
                except Exception:
                    pass
        await asyncio.to_thread(write)

    def summary(self):
        values = sorted(self.samples)
        return dict(successes=self.successes, timeouts=self.timeouts, window_samples=len(values),
                    rtt_p50_ms=round(statistics.median(values), 2) if values else None,
                    rtt_p95_ms=round(values[math.ceil(len(values) * .95) - 1], 2) if values else None,
                    rtt_max_ms=round(max(values), 2) if values else None,
                    rtt_variation_ms=round(statistics.mean(abs(b-a) for a, b in
                        zip(self.samples, list(self.samples)[1:])), 2) if len(values) > 1 else None,
                    max_loop_lag_ms=round(self.max_loop_lag_ms, 2))

    async def lag_loop(self):
        while True:
            before = time.monotonic()
            await asyncio.sleep(.1)
            lag = max(0, (time.monotonic() - before - .1) * 1000)
            self.loop_lag_ms = max(self.loop_lag_ms, lag)
            self.max_loop_lag_ms = max(self.max_loop_lag_ms, lag)

    async def probe(self, ws):
        before = time.monotonic()
        async def round_trip():
            pong = await ws.ping()
            await pong
        try:
            await asyncio.wait_for(round_trip(), self.timeout)
            rtt = (time.monotonic() - before) * 1000
            self.samples.append(rtt)
            self.successes += 1
            status = "ok"
        except TimeoutError:
            self.timeouts += 1
            rtt, status = None, "timeout"
        transport = getattr(ws, "transport", None)
        queued = transport.get_write_buffer_size() if transport else None
        lag, self.loop_lag_ms = self.loop_lag_ms, 0.0
        await self.emit("network_sample", status=status, rtt_ms=round(rtt, 2) if rtt is not None else None,
                        probe_elapsed_ms=round((time.monotonic() - before) * 1000, 2),
                        timeout_seconds=self.timeout,
                        loop_lag_ms=round(lag, 2), write_buffer_bytes=queued, **self.summary())

    async def run(self, ws):
        if not callable(getattr(ws, "ping", None)):
            await self.emit("network_unavailable", reason="socket has no Ping/Pong API")
            return
        lag = asyncio.create_task(self.lag_loop())
        try:
            while True:
                await self.probe(ws)
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.emit("network_probe_error", error_type=type(exc).__name__)
        finally:
            lag.cancel()
            await asyncio.gather(lag, return_exceptions=True)


@asynccontextmanager
async def monitored_socket(context, config, feedback, engine, trace=None, endpoint=""):
    if not config.network_enabled:
        async with context as ws:
            yield ws
        return
    monitor = SocketMonitor(config, feedback, engine, trace, endpoint)
    started = time.monotonic()
    ws, task = None, None
    try:
        async with context as ws:
            await monitor.emit("network_connected", handshake_ms=round((time.monotonic()-started)*1000, 2))
            task = asyncio.create_task(monitor.run(ws))
            try:
                yield ws
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    except Exception as exc:
        await monitor.emit("network_connection_error", phase="connect" if ws is None else "session",
                           error_type=type(exc).__name__)
        raise
    finally:
        await monitor.emit("network_closed", duration_ms=round((time.monotonic()-started)*1000, 2),
                           close_code=getattr(ws, "close_code", None), **monitor.summary())
