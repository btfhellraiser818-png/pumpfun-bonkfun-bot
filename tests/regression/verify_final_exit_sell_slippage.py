"""Verify the last exit attempt can use a looser floor so a crashing coin is sold.

Every exit sell attempt floored the sale at the freshly read price minus
`sell_slippage` (30%). A coin falling faster than that between attempts reverts
each one with 6003 TooLittleSolReceived; after `max_exit_sell_attempts` the bot
gives up and the position is stranded, unmonitored, with its token account's
rent locked. Live this happened twice in a row (Hammy, PIGWIFE), each costing
the whole buy plus manual cleanup.

`trade.final_exit_sell_slippage` sets a separate floor for the last attempt
only. Earlier attempts keep `sell_slippage`, and with the setting absent the
seller is called exactly as before.

Offline machine checks, no network and no funds moved:

  1. The time-based exit uses the override on its last attempt only.
  2. With the setting absent, no attempt passes a slippage override.
  3. With a single attempt allowed, that attempt is the last and uses it.
  4. The tp/sl exit uses the override on its last attempt only.
  5. The real seller floors the sale with the override when given one, and
     with its configured slippage when not.

Usage:
    uv run tests/regression/verify_final_exit_sell_slippage.py
"""

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import WSOL_MINT  # noqa: E402
from interfaces.core import (  # noqa: E402
    ConfirmationStatus,
    Platform,
    TokenInfo,
    TradeFailureReason,
)
from trading import platform_aware  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.platform_aware import PlatformAwareSeller  # noqa: E402
from trading.position import ExitReason, Position  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

FINAL = 0.95
SELL_SLIPPAGE = 0.3
ATTEMPTS = 3
PRICE = 1.0e-6  # SOL per token
QUANTITY = 1_000_000.0
LAMPORTS = 10**9
NOT_PASSED = "default"
NO_CURVE = "stub: no curve"


def _check(label: str, *, passed: bool, detail: str) -> bool:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


def _token_info() -> TokenInfo:
    return TokenInfo(
        name="VerifyFinal",
        symbol="VFINAL",
        uri="",
        mint=Pubkey.default(),
        platform=Platform.PUMP_FUN,
        bonding_curve=Pubkey.default(),
        quote_mint=WSOL_MINT,
    )


@dataclass
class StubSeller:
    """Reverts until the last allowed attempt; records each call's slippage."""

    succeed_on: int
    seen: list = field(default_factory=list)

    async def execute(
        self,
        token_info: TokenInfo,
        token_amount: float,
        token_price: float,
        **kwargs: float,
    ) -> TradeResult:
        self.seen.append(kwargs.get("slippage", NOT_PASSED))
        if len(self.seen) < self.succeed_on:
            return TradeResult(
                success=False,
                platform=token_info.platform,
                tx_signature="stub-reverted",
                error_message="6003 TooLittleSolReceived",
                failure_reason=TradeFailureReason.REVERTED,
            )
        return TradeResult(
            success=True,
            platform=token_info.platform,
            tx_signature="stub-sold",
            amount=token_amount,
            price=token_price,
        )


class StubCurveManager:
    async def calculate_price(self, _pool: Pubkey) -> float:
        return PRICE


def _trader(seller: StubSeller, attempts: int, final: float | None) -> UniversalTrader:
    trader = object.__new__(UniversalTrader)
    trader.wait_time_after_buy = 0
    trader.price_check_interval = 0
    trader.max_exit_sell_attempts = attempts
    if final is not None:
        trader.final_exit_sell_slippage = final
    trader.platform_implementations = SimpleNamespace(
        curve_manager=StubCurveManager(), address_provider=None
    )
    trader.seller = seller
    trader.solana_client = None
    trader.wallet = None
    trader.priority_fee_manager = None
    trader.cleanup_mode = "disabled"
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False
    trader._log_trade = lambda *_a, **_k: None  # noqa: SLF001
    return trader


def _time_based(attempts: int, final: float | None, succeed_on: int) -> list:
    seller = StubSeller(succeed_on=succeed_on)
    buy = TradeResult(
        success=True, platform=Platform.PUMP_FUN, amount=QUANTITY, price=PRICE
    )
    asyncio.run(
        _trader(seller, attempts, final)._handle_time_based_exit(  # noqa: SLF001
            _token_info(), buy
        )
    )
    return seller.seen


def check_time_based_last_attempt_only() -> bool:
    print("\n1. The time-based exit loosens only its last attempt")
    seen = _time_based(ATTEMPTS, FINAL, succeed_on=ATTEMPTS)
    want = [NOT_PASSED, NOT_PASSED, FINAL]
    return _check(
        "slippage per attempt", passed=seen == want, detail=f"{seen} (want {want})"
    )


def check_unset_changes_nothing() -> bool:
    print("\n2. With the setting absent, no attempt passes an override")
    seen = _time_based(ATTEMPTS, None, succeed_on=ATTEMPTS)
    want = [NOT_PASSED] * ATTEMPTS
    return _check(
        "slippage per attempt", passed=seen == want, detail=f"{seen} (want {want})"
    )


def check_single_attempt_is_last() -> bool:
    print("\n3. With one attempt allowed, that attempt uses the override")
    seen = _time_based(1, FINAL, succeed_on=1)
    return _check(
        "slippage per attempt",
        passed=seen == [FINAL],
        detail=f"{seen} (want {[FINAL]})",
    )


def check_tp_sl_last_attempt_only() -> bool:
    print("\n4. The tp/sl exit loosens only its last attempt")
    seller = StubSeller(succeed_on=ATTEMPTS)
    trader = _trader(seller, ATTEMPTS, FINAL)
    position = Position.create_from_buy_result(
        mint=Pubkey.default(),
        symbol="VFINAL",
        entry_price=PRICE,
        quantity=QUANTITY,
        take_profit_percentage=None,
        stop_loss_percentage=0.5,
        max_hold_time=None,
    )

    async def run() -> None:
        for attempt in range(1, ATTEMPTS + 1):
            if await trader._run_exit_attempt(  # noqa: SLF001
                _token_info(), position, ExitReason.STOP_LOSS, PRICE, attempt
            ):
                break

    asyncio.run(run())
    want = [NOT_PASSED, NOT_PASSED, FINAL]
    return _check(
        "slippage per attempt",
        passed=seller.seen == want,
        detail=f"{seller.seen} (want {want})",
    )


def _seller_floor(slippage: float | None) -> int | None:
    """Run the real seller against stubs and return the floor it built."""
    captured: dict = {}

    async def build_sell_instruction(
        _info: object, _user: object, _amount: int, min_out: int, _ap: object
    ) -> list[str]:
        captured["min_out"] = min_out
        return ["stub-instruction"]

    class Curve:
        async def get_pool_state_and_token_program(
            self, *_a: object, **_k: object
        ) -> tuple[dict, Pubkey]:
            raise ConnectionError(NO_CURVE)  # the seller falls back

    implementations = SimpleNamespace(
        address_provider=SimpleNamespace(
            derive_pool_address=lambda _m: Pubkey.default()
        ),
        instruction_builder=SimpleNamespace(
            build_sell_instruction=build_sell_instruction,
            get_required_accounts_for_sell=lambda *_a, **_k: [],
            get_sell_compute_unit_limit=lambda _o: 100_000,
        ),
        curve_manager=Curve(),
    )
    platform_aware.get_platform_implementations = lambda _p, _c: implementations
    # Skips the retry budget's waits; the stub fails every read anyway.
    stub_read = Curve().get_pool_state_and_token_program
    platform_aware._read_pool_state_with_retry = stub_read  # noqa: SLF001

    async def no_fee(_accounts: list) -> None:
        return None

    async def send(*_a: object, **_k: object) -> str:
        return "stub-signature"

    async def confirm(*_a: object, **_k: object) -> ConfirmationStatus:
        return ConfirmationStatus.SUCCESS

    client = SimpleNamespace(
        build_and_send_transaction=send,
        confirm_transaction=lambda *_a, **_k: asyncio.sleep(0, result=True),
        confirm_transaction_detailed=confirm,
    )
    seller = PlatformAwareSeller(
        client,
        SimpleNamespace(pubkey=Pubkey.default(), keypair=None),
        SimpleNamespace(calculate_priority_fee=no_fee),
        slippage=SELL_SLIPPAGE,
        max_retries=1,
    )
    kwargs = {} if slippage is None else {"slippage": slippage}
    asyncio.run(
        seller.execute(
            _token_info(), token_amount=QUANTITY, token_price=PRICE, **kwargs
        )
    )
    return captured.get("min_out")


def check_seller_applies_override() -> bool:
    print("\n5. The real seller floors the sale with the override when given one")
    expected = QUANTITY * PRICE * LAMPORTS
    with_override = _seller_floor(FINAL)
    without = _seller_floor(None)
    want_override = max(1, int(expected * (1 - FINAL)))
    want_default = max(1, int(expected * (1 - SELL_SLIPPAGE)))
    return all(
        [
            _check(
                "floor with override (lamports)",
                passed=with_override == want_override,
                detail=f"{with_override} (want {want_override})",
            ),
            _check(
                "floor without (lamports)",
                passed=without == want_default,
                detail=f"{without} (want {want_default})",
            ),
        ]
    )


def main() -> None:
    print("=" * 72)
    print("Verifying the last exit attempt can use a looser floor")
    print("=" * 72)

    results = []
    for check in (
        check_time_based_last_attempt_only,
        check_unset_changes_nothing,
        check_single_attempt_is_last,
        check_tp_sl_last_attempt_only,
        check_seller_applies_override,
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
