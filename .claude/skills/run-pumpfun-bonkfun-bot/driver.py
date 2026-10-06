"""Drive the sniper bot without moving funds.

Subcommands:
  verify [script ...]   run the offline regression verifiers (all, or the named ones)
  config [yaml ...]     load and validate bot configs exactly as bot_runner does
  listen [options]      run a real listener against mainnet and print each coin it
                        detects; never builds or sends a transaction

Run from the repo root:
    uv run .claude/skills/run-pumpfun-bonkfun-bot/driver.py <subcommand> ...
"""

import argparse
import asyncio
import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

import config_loader  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from monitoring.listener_factory import ListenerFactory  # noqa: E402
from utils.logger import install_secret_redaction  # noqa: E402

# Placeholders used only to validate a config whose ${VAR}s are not exported.
# Never sent anywhere: `config` only loads and validates.
PLACEHOLDERS = {
    "SOLANA_NODE_RPC_ENDPOINT": "https://placeholder.invalid",
    "SOLANA_NODE_WSS_ENDPOINT": "wss://placeholder.invalid",
    "SOLANA_PRIVATE_KEY": "placeholder",
    "GEYSER_ENDPOINT": "https://placeholder.invalid",
    "GEYSER_API_TOKEN": "placeholder",
    "GEYSER_AUTH_TYPE": "x-token",
}


def cmd_verify(args: argparse.Namespace) -> int:
    cmd = ["uv", "run", "tests/regression/run_all.py", *args.scripts]
    return subprocess.call(cmd, cwd=ROOT)  # noqa: S603


def cmd_config(args: argparse.Namespace) -> int:
    # config_loader reads the YAML's env_file with override=True. Skip it so a
    # stray .env can neither be read here nor override the exported values.
    config_loader.load_dotenv = lambda *_a, **_k: None
    filled = [k for k in PLACEHOLDERS if k not in os.environ]
    for k in filled:
        os.environ[k] = PLACEHOLDERS[k]
    if filled:
        print(f"placeholders for unset: {', '.join(filled)}")

    paths = args.paths or sorted(str(p) for p in (ROOT / "bots").glob("*.yaml"))
    failed = 0
    for path in paths:
        try:
            cfg = config_loader.load_bot_config(path)
            t, f = cfg["trade"], cfg["filters"]
            print(
                f"OK   {Path(path).name}: enabled={cfg.get('enabled', True)} "
                f"platform={cfg.get('platform', 'pump_fun')} "
                f"listener={f['listener_type']} buy={t['buy_amount']} SOL "
                f"buy_slippage={t['buy_slippage']} exit={t.get('exit_strategy')} "
                f"extreme_fast={t.get('extreme_fast_mode', False)} "
                f"skip_mayhem={f.get('skip_mayhem_mode', False)}"
            )
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {Path(path).name}: {e}")
    return 1 if failed else 0


async def _listen(args: argparse.Namespace) -> int:
    install_secret_redaction()
    wss = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")
    if not wss:
        print("SOLANA_NODE_WSS_ENDPOINT is not set", file=sys.stderr)
        return 2
    platform = Platform(args.platform)
    listener = ListenerFactory.create_listener(
        listener_type=args.listener, wss_endpoint=wss, platforms=[platform]
    )
    seen: list = []
    done = asyncio.Event()
    start = time.monotonic()

    async def on_token(info: TokenInfo) -> None:
        seen.append(info)
        print(
            f"COIN {len(seen)} +{time.monotonic() - start:5.1f}s "
            f"{info.symbol!r} mint={info.mint} creator={info.creator} "
            f"mayhem={info.is_mayhem_mode} quote={info.quote_mint} "
            f"state_from_event={getattr(info, 'state_from_event', None)}",
            flush=True,
        )
        if len(seen) >= args.count:
            done.set()

    print(f"LISTENING listener={args.listener} platform={args.platform}", flush=True)
    task = asyncio.create_task(listener.listen_for_tokens(on_token))
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(done.wait(), timeout=args.timeout)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    print(f"DONE coins={len(seen)} in {time.monotonic() - start:.1f}s", flush=True)
    return 0 if seen else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify")
    v.add_argument("scripts", nargs="*")
    c = sub.add_parser("config")
    c.add_argument("paths", nargs="*")
    lst = sub.add_parser("listen")
    lst.add_argument("--listener", default="logs", choices=["logs", "blocks"])
    lst.add_argument("--platform", default="pump_fun")
    lst.add_argument("--count", type=int, default=3)
    lst.add_argument("--timeout", type=float, default=90.0)
    a = p.parse_args()
    if a.cmd == "verify":
        return cmd_verify(a)
    if a.cmd == "config":
        return cmd_config(a)
    return asyncio.run(_listen(a))


if __name__ == "__main__":
    sys.exit(main())
