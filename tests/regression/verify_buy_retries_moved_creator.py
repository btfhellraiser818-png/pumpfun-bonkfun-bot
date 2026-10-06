"""Verify a buy retries once when the curve's creator moved after the create.

A coin can hand its bonding curve's creator to a fee-sharing config in a
transaction *after* the create. The listener only sees the create, so a buy
built from the CreateEvent's creator derives the wrong creator_vault and reverts
with ConstraintSeeds (2006). The fix for the same-transaction case
(`verify_creator_migration_from_logs.py`) cannot see it. Live, this cost a
mainnet buy (Sparrows, 88NZzH8N...): the curve's creator had become
`PDA(["sharing-config", mint])` before the buy landed.

After a confirmed revert the buyer now re-reads the curve and, only if the
creator changed, rebuilds the buy and sends it once more.

Offline machine checks, no network and no funds moved:

  1. A revert with a moved creator retries once, with the curve's creator_vault,
     and reports the second buy's success.
  2. A revert with an unchanged creator is not retried.
  3. An unconfirmed buy is neither re-read nor retried: it may have landed.
  4. A retry that reverts again stops there — at most two submissions.
  5. With `trade.retry_moved_creator: false` a moved creator is neither re-read
     nor retried: the revert is the result.
  6. Outside extreme_fast_mode the buy reads the curve anyway, so its first
     submission already uses the curve's creator.

Usage:
    uv run tests/regression/verify_buy_retries_moved_creator.py
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
from interfaces.core import ConfirmationStatus, Platform, TokenInfo  # noqa: E402
from platforms.pumpfun.address_provider import (  # noqa: E402
    PumpFunAddresses,
    PumpFunAddressProvider,
)
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
REVERTED = ConfirmationStatus.REVERTED
SUCCESS = ConfirmationStatus.SUCCESS
UNCONFIRMED = ConfirmationStatus.UNCONFIRMED
MAX_SENDS = 2  # the original buy plus one retry


def _check(label: str, *, passed: bool, detail: str) -> bool:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


def _token_info() -> TokenInfo:
    parser = PumpFunEventParser(
        idl_parser=get_idl_manager().get_parser(Platform.PUMP_FUN)
    )
    logs = json.loads(FIXTURE.read_text())["meta"]["logMessages"]
    return parser.parse_token_creation_from_logs(logs, signature="fixture")


class _Client:
    """Answers confirmations from a script; records nothing on the network."""

    def __init__(self, statuses: list[ConfirmationStatus]) -> None:
        self.statuses = list(statuses)
        self.sends = 0

    async def build_and_send_transaction(self, *_a: object, **_k: object) -> str:
        self.sends += 1
        return f"SIG{self.sends}"

    async def confirm_transaction_detailed(
        self, *_a: object, **_k: object
    ) -> ConfirmationStatus:
        return self.statuses.pop(0)

    async def get_buy_transaction_details(
        self, *_a: object, **_k: object
    ) -> tuple[int, int]:
        return 2_000_000_000, 70_000


class _CurveManager:
    """Returns the curve with whichever creator the scenario sets."""

    def __init__(self, creator: Pubkey) -> None:
        self.creator = creator
        self.reads = 0

    async def get_pool_state_and_token_program(
        self, *_a: object, **_k: object
    ) -> tuple[dict, Pubkey]:
        self.reads += 1
        return await self.get_pool_state(), SystemAddresses.TOKEN_2022_PROGRAM

    async def get_pool_state(self, *_a: object, **_k: object) -> dict:
        return {
            "creator": str(self.creator),
            "is_mayhem_mode": False,
            "is_cashback_coin": False,
            "quote_mint": WSOL_MINT,
            "price_per_token": 2.8e-8,
        }


def _run(
    statuses: list[ConfirmationStatus],
    curve_creator: Pubkey | None,
    *,
    retry_moved_creator: bool = True,
    extreme_fast_mode: bool = True,
) -> tuple[object, _Client, _CurveManager, list[Pubkey], TokenInfo]:
    """Run one buy; curve_creator None means the curve still holds the event's."""
    token_info = _token_info()
    curve = _CurveManager(curve_creator or token_info.creator)
    client = _Client(statuses)
    vaults_built: list[Pubkey] = []

    async def build_buy_instruction(info: TokenInfo, *_a: object) -> list[str]:
        vaults_built.append(info.creator_vault)
        return ["stub-instruction"]

    implementations = SimpleNamespace(
        address_provider=PROVIDER,
        instruction_builder=SimpleNamespace(
            build_buy_instruction=build_buy_instruction,
            get_required_accounts_for_buy=lambda *_a, **_k: [],
            get_buy_compute_unit_limit=lambda _override: 100_000,
        ),
        curve_manager=curve,
    )
    platform_aware.get_platform_implementations = lambda _p, _c: implementations

    async def no_fee(_accounts: list) -> None:
        return None

    buyer = PlatformAwareBuyer(
        client,
        SimpleNamespace(pubkey=TRADER, keypair=None),
        SimpleNamespace(calculate_priority_fee=no_fee),
        amount=0.002,
        slippage=0.3,
        max_retries=1,
        extreme_fast_mode=extreme_fast_mode,
        retry_moved_creator=retry_moved_creator,
    )
    result = asyncio.run(buyer.execute(token_info))
    return result, client, curve, vaults_built, token_info


def check_moved_creator_retries() -> bool:
    print("\n1. A revert with a moved creator retries once with the curve's vault")
    event_info = _token_info()
    sharing_config = PumpFunAddresses.find_sharing_config(event_info.mint)
    result, client, _curve, vaults, _info = _run([REVERTED, SUCCESS], sharing_config)
    want = PROVIDER.derive_creator_vault(sharing_config)
    return all(
        [
            _check(
                "submissions",
                passed=client.sends == MAX_SENDS,
                detail=f"{client.sends} (want 2)",
            ),
            _check(
                "retry's creator_vault",
                passed=len(vaults) == MAX_SENDS and vaults[1] == want,
                detail="derived from the sharing-config creator"
                if len(vaults) == MAX_SENDS and vaults[1] == want
                else f"{vaults}",
            ),
            _check(
                "TradeResult.success",
                passed=result.success,
                detail=str(result.success),
            ),
        ]
    )


def check_same_creator_does_not_retry() -> bool:
    print("\n2. A revert with an unchanged creator is not retried")
    result, client, _curve, _vaults, _info = _run([REVERTED], None)
    return _check(
        "submissions / success",
        passed=client.sends == 1 and not result.success,
        detail=f"{client.sends} / {result.success} (want 1 / False)",
    )


def check_unconfirmed_is_not_retried() -> bool:
    print("\n3. An unconfirmed buy is neither re-read nor retried")
    event_info = _token_info()
    moved = PumpFunAddresses.find_sharing_config(event_info.mint)
    result, client, curve, _vaults, _info = _run([UNCONFIRMED], moved)
    return _check(
        "submissions / curve reads / success",
        passed=client.sends == 1 and curve.reads == 0 and not result.success,
        detail=f"{client.sends} / {curve.reads} / {result.success} "
        f"(want 1 / 0 / False — it may have landed)",
    )


def check_retry_is_bounded() -> bool:
    print("\n4. A retry that reverts again stops — at most two submissions")
    event_info = _token_info()
    moved = PumpFunAddresses.find_sharing_config(event_info.mint)
    result, client, _curve, _vaults, _info = _run([REVERTED, REVERTED], moved)
    return _check(
        "submissions / success",
        passed=client.sends == MAX_SENDS and not result.success,
        detail=f"{client.sends} / {result.success} (want 2 / False)",
    )


def check_retry_can_be_disabled() -> bool:
    print("\n5. retry_moved_creator=False: a moved creator is not re-read or retried")
    event_info = _token_info()
    moved = PumpFunAddresses.find_sharing_config(event_info.mint)
    result, client, curve, _vaults, _info = _run(
        [REVERTED], moved, retry_moved_creator=False
    )
    return _check(
        "submissions / curve reads / success",
        passed=client.sends == 1 and curve.reads == 0 and not result.success,
        detail=f"{client.sends} / {curve.reads} / {result.success} "
        f"(want 1 / 0 / False)",
    )


def check_regular_mode_uses_curve_creator() -> bool:
    print("\n6. Regular mode builds the first buy with the curve's creator")
    event_info = _token_info()
    moved = PumpFunAddresses.find_sharing_config(event_info.mint)
    _result, client, _curve, vaults, _info = _run(
        [SUCCESS], moved, extreme_fast_mode=False
    )
    want = PROVIDER.derive_creator_vault(moved)
    return _check(
        "first buy's creator_vault / submissions",
        passed=bool(vaults) and vaults[0] == want and client.sends == 1,
        detail=f"{'curve creator' if vaults and vaults[0] == want else vaults} / "
        f"{client.sends} (want curve creator / 1)",
    )


def main() -> None:
    print("=" * 72)
    print("Verifying a buy retries once when the curve's creator moved")
    print("=" * 72)

    results = []
    for check in (
        check_moved_creator_retries,
        check_same_creator_does_not_retry,
        check_unconfirmed_is_not_retried,
        check_retry_is_bounded,
        check_retry_can_be_disabled,
        check_regular_mode_uses_curve_creator,
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
