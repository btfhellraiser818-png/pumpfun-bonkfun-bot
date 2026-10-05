"""Verify `filters.skip_mayhem_mode` keeps the sniper out of mayhem-mode coins.

A mayhem-mode curve's virtual reserves can price a position above the real SOL
the curve holds. pump.fun then reverts every sell of it with Overflow (6024),
whatever the slippage, and the tokens are stranded. Live, this stranded a
mainnet buy ($SHARK, 3M4sqA3j...): the curve held 0.00157 SOL of real reserves
against a position priced at 0.0025 SOL. The fixture is that coin's create.

Offline machine checks, no network and no funds moved:

  1. The logs listener's parser reads the mayhem flag off the create alone,
     and an ordinary create parses as not mayhem.
  2. With the filter on, a mayhem coin is dropped before the buyer is called.
  3. With the filter on, an ordinary coin is still bought.
  4. With the filter off (the default), a mayhem coin is still bought.
  5. bot_runner passes `filters.skip_mayhem_mode` through to the trader.

Usage:
    uv run tests/regression/verify_skip_mayhem_mode.py
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.pubkeys import WSOL_MINT  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from platforms.pumpfun.event_parser import PumpFunEventParser  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

DECODE_DIR = PROJECT_ROOT / "cookbook" / "pumpfun" / "decode"
MAYHEM_FIXTURE = DECODE_DIR / "raw_create_v2_mayhem_from_gettransaction.json"
ORDINARY_FIXTURE = DECODE_DIR / "raw_create_tx_from_blocksubscribe.json"


def _check(label: str, *, passed: bool, detail: str) -> bool:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


def _parse(path: Path) -> TokenInfo | None:
    parser = PumpFunEventParser(
        idl_parser=get_idl_manager().get_parser(Platform.PUMP_FUN)
    )
    logs = json.loads(path.read_text())["meta"]["logMessages"]
    return parser.parse_token_creation_from_logs(logs, signature=path.stem)


def _buys(token_info: TokenInfo, *, skip_mayhem_mode: bool) -> int:
    """Run the trader's token handler once; return how often it bought."""
    calls: list[TokenInfo] = []

    async def execute(info: TokenInfo) -> TradeResult:
        calls.append(info)
        return TradeResult(success=False, platform=Platform.PUMP_FUN)

    async def no_op(*_a: object) -> None:
        return None

    trader = object.__new__(UniversalTrader)
    trader.platform = Platform.PUMP_FUN
    trader.allowed_quote_mints = None
    trader.quote_amounts = {WSOL_MINT: 0.002}
    trader.skip_mayhem_mode = skip_mayhem_mode
    trader.extreme_fast_mode = True
    trader.yolo_mode = False
    trader.buyer = SimpleNamespace(execute=execute)
    trader._handle_failed_buy = no_op  # noqa: SLF001
    trader._handle_successful_buy = no_op  # noqa: SLF001
    asyncio.run(trader._handle_token(token_info))  # noqa: SLF001
    return len(calls)


def check_flag_parsed() -> bool:
    print("\n1. The create alone carries the mayhem flag")
    mayhem, ordinary = _parse(MAYHEM_FIXTURE), _parse(ORDINARY_FIXTURE)
    return _check(
        "mayhem / ordinary is_mayhem_mode",
        passed=bool(mayhem and mayhem.is_mayhem_mode)
        and bool(ordinary and not ordinary.is_mayhem_mode),
        detail=f"{mayhem and mayhem.is_mayhem_mode} / "
        f"{ordinary and ordinary.is_mayhem_mode} (want True / False)",
    )


def check_mayhem_skipped() -> bool:
    print("\n2. Filter on: a mayhem coin is never handed to the buyer")
    buys = _buys(_parse(MAYHEM_FIXTURE), skip_mayhem_mode=True)
    return _check("buys", passed=buys == 0, detail=f"{buys} (want 0)")


def check_ordinary_still_bought() -> bool:
    print("\n3. Filter on: an ordinary coin is still bought")
    buys = _buys(_parse(ORDINARY_FIXTURE), skip_mayhem_mode=True)
    return _check("buys", passed=buys == 1, detail=f"{buys} (want 1)")


def check_default_unchanged() -> bool:
    print("\n4. Filter off: a mayhem coin is still bought")
    buys = _buys(_parse(MAYHEM_FIXTURE), skip_mayhem_mode=False)
    return _check("buys", passed=buys == 1, detail=f"{buys} (want 1)")


def check_runner_passes_setting() -> bool:
    print("\n5. bot_runner reads filters.skip_mayhem_mode")
    source = (PROJECT_ROOT / "src" / "bot_runner.py").read_text()
    wired = 'skip_mayhem_mode=cfg["filters"].get("skip_mayhem_mode"' in source
    return _check(
        "keyword passed to UniversalTrader",
        passed=wired,
        detail="present" if wired else "missing",
    )


def main() -> None:
    print("=" * 72)
    print("Verifying filters.skip_mayhem_mode keeps mayhem coins out")
    print("=" * 72)

    results = []
    for check in (
        check_flag_parsed,
        check_mayhem_skipped,
        check_ordinary_still_bought,
        check_default_unchanged,
        check_runner_passes_setting,
    ):
        try:
            results.append(check())
        except Exception as e:  # noqa: BLE001
            results.append(_check(check.__name__, passed=False, detail=f"raised {e!r}"))

    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
