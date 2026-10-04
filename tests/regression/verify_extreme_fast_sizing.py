"""Verify extreme_fast_mode sizes a buy from `buy_amount`, not a fixed token count.

extreme_fast_mode skips the curve read, so it used to buy a fixed
`extreme_fast_token_amount` (20 in every shipped config) and back-derive a price
from it. At a fresh coin's real price, 20 tokens cost a few hundred lamports, so
a 0.0001 SOL `buy_amount` bought about 1/200th of what it asked for, and the log
reported a price ~150x the real one. buy_v2 then buys exactly the minimum it is
given, so slippage shaved another 30% off.

The CreateEvent carries the opening virtual reserves, and the pre-buy curve
refresh reads them too, so the price is known with no extra RPC call. The buy
now spends `buy_amount` at that price; the fixed count is only the fallback for
a TokenInfo with no reserves.

Offline machine checks, no network and no funds moved:

  1. The logs parser carries both virtual reserves from the CreateEvent.
  2. An event-sourced buy sizes its token count from `buy_amount` at the
     reserves' price, and caps spend at `buy_amount` plus slippage.
  3. A TokenInfo with no reserves still falls back to the fixed token count.
  4. The curve refresh copies the curve's reserves onto the TokenInfo, so an
     instruction-parsed token is sized from them too.

Usage:
    uv run tests/regression/verify_extreme_fast_sizing.py
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import WSOL_MINT, SystemAddresses  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddressProvider  # noqa: E402
from platforms.pumpfun.event_parser import PumpFunEventParser  # noqa: E402
from trading import platform_aware  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

FIXTURE = (
    PROJECT_ROOT
    / "cookbook"
    / "pumpfun"
    / "decode"
    / "raw_create_tx_from_blocksubscribe.json"
)

PROVIDER = PumpFunAddressProvider()
TRADER = Pubkey.from_string("11111111111111111111111111111112")
BUY_AMOUNT = 0.0001  # SOL
SLIPPAGE = 0.3
FIXED_TOKENS = 20
LAMPORTS = 10**9
TOKEN_UNITS = 10**6
# A fresh pump.fun curve: 30 SOL and 1.073B tokens of virtual reserves.
FRESH_QUOTE_RESERVES = 30 * LAMPORTS
FRESH_TOKEN_RESERVES = 1_073_000_000 * TOKEN_UNITS


def _check(label: str, *, passed: bool, detail: str) -> bool:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


def _event_sourced_token_info() -> TokenInfo:
    parser = PumpFunEventParser(
        idl_parser=get_idl_manager().get_parser(Platform.PUMP_FUN)
    )
    logs = json.loads(FIXTURE.read_text())["meta"]["logMessages"]
    return parser.parse_token_creation_from_logs(logs, signature="fixture")


class _StubClient:
    async def build_and_send_transaction(self, *_a: object, **_k: object) -> str:
        return "STUB_SIGNATURE"

    async def confirm_transaction(self, *_a: object, **_k: object) -> bool:
        return False


class _CurveManager:
    """Returns a fresh curve; the refresh path reads it."""

    async def get_pool_state_and_token_program(
        self, *_a: object, **_k: object
    ) -> tuple[dict, Pubkey]:
        state = {
            "creator": str(TRADER),
            "is_mayhem_mode": False,
            "is_cashback_coin": False,
            "quote_mint": WSOL_MINT,
            "virtual_token_reserves": FRESH_TOKEN_RESERVES,
            "virtual_quote_reserves": FRESH_QUOTE_RESERVES,
        }
        return state, SystemAddresses.TOKEN_2022_PROGRAM


def _run_buy(token_info: TokenInfo) -> dict:
    """Run one extreme_fast_mode buy and capture the buy instruction's amounts."""
    captured: dict = {}

    async def build_buy_instruction(
        _token_info: object,
        _user: object,
        amount_in: int,
        minimum_amount_out: int,
        _provider: object,
    ) -> list[str]:
        captured["max_quote_raw"] = amount_in
        captured["tokens_raw"] = minimum_amount_out
        return ["stub-instruction"]

    implementations = SimpleNamespace(
        address_provider=PROVIDER,
        instruction_builder=SimpleNamespace(
            build_buy_instruction=build_buy_instruction,
            get_required_accounts_for_buy=lambda *_a, **_k: [],
            get_buy_compute_unit_limit=lambda _override: 100_000,
        ),
        curve_manager=_CurveManager(),
    )
    platform_aware.get_platform_implementations = lambda _p, _c: implementations

    async def no_fee(_accounts: list) -> None:
        return None

    buyer = PlatformAwareBuyer(
        _StubClient(),
        SimpleNamespace(pubkey=TRADER, keypair=None),
        SimpleNamespace(calculate_priority_fee=no_fee),
        amount=BUY_AMOUNT,
        slippage=SLIPPAGE,
        max_retries=1,
        extreme_fast_token_amount=FIXED_TOKENS,
        extreme_fast_mode=True,
    )
    asyncio.run(buyer.execute(token_info))
    return captured


def _expected_tokens_raw(quote_reserves: int, token_reserves: int) -> int:
    price = (quote_reserves / token_reserves) * TOKEN_UNITS / LAMPORTS
    return int(BUY_AMOUNT / price * (1 - SLIPPAGE) * TOKEN_UNITS)


def check_parser_carries_reserves() -> bool:
    print("\n1. The logs parser carries both virtual reserves from the CreateEvent")
    info = _event_sourced_token_info()
    has_both = bool(info.virtual_token_reserves) and bool(info.virtual_quote_reserves)
    return _check(
        "virtual_token_reserves / virtual_quote_reserves",
        passed=has_both,
        detail=f"{info.virtual_token_reserves} / {info.virtual_quote_reserves}",
    )


def check_event_sourced_buy_spends_buy_amount() -> bool:
    print("\n2. An event-sourced buy is sized from buy_amount at the reserves' price")
    info = _event_sourced_token_info()
    captured = _run_buy(info)
    want_tokens = _expected_tokens_raw(
        info.virtual_quote_reserves, info.virtual_token_reserves
    )
    want_cap = int(BUY_AMOUNT * LAMPORTS * (1 + SLIPPAGE))
    tokens_ok = captured.get("tokens_raw") == want_tokens
    cap_ok = captured.get("max_quote_raw") == want_cap
    fixed_raw = int(FIXED_TOKENS * (1 - SLIPPAGE) * TOKEN_UNITS)
    return all(
        [
            _check(
                "tokens bought (raw)",
                passed=tokens_ok,
                detail=f"{captured.get('tokens_raw')} (want {want_tokens}; the "
                f"fixed count would be {fixed_raw})",
            ),
            _check(
                "spend cap (lamports)",
                passed=cap_ok,
                detail=f"{captured.get('max_quote_raw')} (want {want_cap})",
            ),
        ]
    )


def check_no_reserves_falls_back_to_fixed_count() -> bool:
    print("\n3. A TokenInfo with no reserves still buys the fixed token count")
    info = _event_sourced_token_info()
    info.virtual_token_reserves = None
    info.virtual_quote_reserves = None
    captured = _run_buy(info)
    want = int(FIXED_TOKENS * (1 - SLIPPAGE) * TOKEN_UNITS)
    return _check(
        "tokens bought (raw)",
        passed=captured.get("tokens_raw") == want,
        detail=f"{captured.get('tokens_raw')} (want {want})",
    )


def check_refresh_supplies_reserves() -> bool:
    print("\n4. The curve refresh supplies reserves to an instruction-parsed token")
    info = _event_sourced_token_info()
    info.virtual_token_reserves = None
    info.virtual_quote_reserves = None
    info.state_from_event = False  # forces the pre-buy curve refresh
    captured = _run_buy(info)
    want = _expected_tokens_raw(FRESH_QUOTE_RESERVES, FRESH_TOKEN_RESERVES)
    return _check(
        "tokens bought (raw)",
        passed=captured.get("tokens_raw") == want,
        detail=f"{captured.get('tokens_raw')} (want {want}, from the curve's reserves)",
    )


def main() -> None:
    print("=" * 72)
    print("Verifying extreme_fast_mode sizes buys from buy_amount")
    print("=" * 72)

    results = []
    for check in (
        check_parser_carries_reserves,
        check_event_sourced_buy_spends_buy_amount,
        check_no_reserves_falls_back_to_fixed_count,
        check_refresh_supplies_reserves,
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
