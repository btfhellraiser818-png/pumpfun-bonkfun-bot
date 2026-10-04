"""Verify a WebSocket listener stops when cancelled while frames keep arriving.

Both WebSocket listeners read with a 30-second timeout. They used
`asyncio.wait_for(websocket.recv(), timeout=30)`, and on Python 3.11
`wait_for` drops a cancellation that lands in the same loop iteration as the
inner `recv()` completing: it returns the frame instead of raising
`CancelledError`. pump.fun's logs subscription delivers frames continuously, so
when single-token mode cancelled the listener after picking its coin, the
cancellation was lost most of the time. `_wait_for_token` then waited forever on
a listener that kept detecting coins, and the bot never bought. Reading under
`async with asyncio.timeout(30)` keeps the timeout and lets the cancellation
through.

Offline machine checks, no network and no funds moved:

  1. No listener under `src/monitoring/` wraps `recv()` in `asyncio.wait_for`.
  2. The logs listener's read loop stops on every one of 30 cancellations while
     a frame is always ready.
  3. The blocks listener does the same.

Usage:
    uv run tests/regression/verify_listener_cancel_under_flood.py
"""

import asyncio
import contextlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# The real connect() imports these; the listeners' except clauses need them.
import websockets.asyncio.client  # noqa: E402
import websockets.exceptions  # noqa: E402, F401

from interfaces.core import Platform  # noqa: E402
from monitoring.universal_block_listener import UniversalBlockListener  # noqa: E402
from monitoring.universal_logs_listener import UniversalLogsListener  # noqa: E402

TRIALS = 30
CANCEL_TIMEOUT = 2  # a listener that honours cancellation stops in milliseconds
# A frame neither listener treats as a coin, so the loop just reads again.
IDLE_FRAME = json.dumps({"jsonrpc": "2.0", "id": 99, "result": 1})


def _check(label: str, *, passed: bool, detail: str) -> bool:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


def _flooding_socket() -> SimpleNamespace:
    """A websocket that always has a frame ready, like pump.fun's firehose."""

    async def recv() -> str:
        await asyncio.sleep(0)
        return IDLE_FRAME

    return SimpleNamespace(recv=recv)


async def _stops_on_cancel(listener: object) -> bool:
    """Cancel a read loop mid-flood and report whether it stopped."""

    async def read_loop() -> None:
        socket = _flooding_socket()
        while True:
            await listener._wait_for_token_creation(socket)  # noqa: SLF001

    task = asyncio.create_task(read_loop())
    await asyncio.sleep(0.01)
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=CANCEL_TIMEOUT)
    if done:
        return task.cancelled()
    # Lost the cancellation: keep cancelling so the check itself can finish.
    for _ in range(100):
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.05)
        if done:
            break
    return False


async def _check_listener(name: str, listener: object) -> bool:
    for trial in range(1, TRIALS + 1):
        if not await _stops_on_cancel(listener):
            return _check(
                "cancellations honoured",
                passed=False,
                detail=f"lost on trial {trial}/{TRIALS} — {name} kept reading, "
                f"so single-token mode would never buy",
            )
    return _check(
        "cancellations honoured",
        passed=True,
        detail=f"{TRIALS}/{TRIALS} — {name} stops when single-token mode is done "
        f"with it",
    )


def check_no_wait_for_around_recv() -> bool:
    print("\n1. No listener wraps recv() in asyncio.wait_for")
    pattern = re.compile(r"wait_for\(\s*\w+\.recv\(")
    offenders = [
        str(path.relative_to(PROJECT_ROOT))
        for path in sorted((PROJECT_ROOT / "src" / "monitoring").rglob("*.py"))
        if pattern.search(path.read_text())
    ]
    return _check(
        "files using wait_for(...recv())",
        passed=not offenders,
        detail=", ".join(offenders) if offenders else "none",
    )


async def check_logs_listener() -> bool:
    print("\n2. The logs listener stops on cancel while frames keep arriving")
    return await _check_listener(
        "the logs listener",
        UniversalLogsListener(
            wss_endpoint="wss://stub.invalid", platforms=[Platform.PUMP_FUN]
        ),
    )


async def check_block_listener() -> bool:
    print("\n3. The blocks listener stops on cancel while frames keep arriving")
    return await _check_listener(
        "the blocks listener",
        UniversalBlockListener(
            wss_endpoint="wss://stub.invalid", platforms=[Platform.PUMP_FUN]
        ),
    )


async def main() -> None:
    print("=" * 72)
    print("Verifying listeners honour cancellation under a frame flood")
    print("=" * 72)

    results = [
        check_no_wait_for_around_recv(),
        await check_logs_listener(),
        await check_block_listener(),
    ]

    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
