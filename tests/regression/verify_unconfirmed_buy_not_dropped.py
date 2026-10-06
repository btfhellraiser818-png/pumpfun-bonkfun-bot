"""Verify a trade whose outcome is unknown is re-checked, never assumed failed.

One HTTP 429 or read timeout among the ~90 signature-status polls of a buy's
confirmation raised inside solana-py, and `confirm_transaction_detailed` turned
it straight into UNCONFIRMED without reading the chain. The buyer then reported
any non-SUCCESS as a failed buy, so a buy that landed was never sold, never
reached session cleanup, and under `cleanup.mode: on_fail` with force burn was
burned. Separately, a sendTransaction that timed out after the request went out
raised without its signature, so the seller reported SUBMIT_FAILED ("nothing
reached the chain") and the exit loop resent a fresh sell without asking.

Offline machine checks, no network and no funds moved:

  1. Status polling that raises still reads the transaction back.
  2. An unconfirmed buy that the re-check finds landed is a successful buy.
  3. An unconfirmed buy still invisible on chain, with tokens in the wallet,
     is a successful buy.
  4. An unconfirmed buy with nothing on chain or in the wallet fails as
     UNCONFIRMED and keeps its signature.
  5. The trader never runs on_fail cleanup for an UNCONFIRMED buy and tracks its
     mint for session cleanup; a REVERTED buy still gets on_fail cleanup.
  6. A send that times out after the request carries its signature; one that
     never connected does not.
  7. The seller reports such a send as UNCONFIRMED with the signature, and a
     send that never connected as SUBMIT_FAILED.
  8. The buyer reports such a send as UNCONFIRMED with the signature.

Usage:
    uv run tests/regression/verify_unconfirmed_buy_not_dropped.py
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import httpx2  # noqa: E402
from solana.exceptions import SolanaRpcException  # noqa: E402
from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from core.client import SolanaClient  # noqa: E402
from core.pubkeys import WSOL_MINT  # noqa: E402
from interfaces.core import (  # noqa: E402
    ConfirmationStatus,
    Platform,
    TokenInfo,
    TradeFailureReason,
)
from trading import platform_aware, universal_trader  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

SIGNATURE = "5" * 88
MINT = Pubkey.from_string("So11111111111111111111111111111111111111113")
CURVE = Pubkey.from_string("So11111111111111111111111111111111111111114")
UNCONFIRMED = ConfirmationStatus.UNCONFIRMED


def _check(label: str, *, passed: bool, detail: str) -> bool:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


async def _noop(*_a: object, **_k: object) -> None:
    return None


def _token_info() -> TokenInfo:
    return TokenInfo(
        name="Unconfirmed",
        symbol="UNCF",
        uri="",
        mint=MINT,
        platform=Platform.PUMP_FUN,
        bonding_curve=CURVE,
        associated_bonding_curve=CURVE,
        creator=CURVE,
        creator_vault=CURVE,
        quote_mint=WSOL_MINT,
        state_from_event=True,
        virtual_token_reserves=1_073_000_000_000_000,
        virtual_quote_reserves=30_000_000_000,
    )


def _wrapped(inner: Exception) -> SolanaRpcException:
    """Wrap an httpx2 error the way solana-py's decorators do (`raise ... from`)."""
    wrapped = SolanaRpcException(inner, _noop, None, SimpleNamespace())
    wrapped.__cause__ = inner
    return wrapped


class StubBuyClient:
    """A buy whose confirmation comes back UNCONFIRMED."""

    def __init__(
        self,
        recheck: ConfirmationStatus,
        balance_raw: int,
        send_error: Exception | None = None,
    ) -> None:
        self.recheck = recheck
        self.balance_raw = balance_raw
        self.send_error = send_error

    async def build_and_send_transaction(self, *_a: object, **_k: object) -> str:
        if self.send_error:
            raise self.send_error
        return SIGNATURE

    async def confirm_transaction_detailed(self, *_a: object) -> ConfirmationStatus:
        return UNCONFIRMED

    async def verify_transaction_status(self, *_a: object) -> ConfirmationStatus:
        return self.recheck

    async def get_buy_transaction_details(self, *_a: object, **_k: object) -> tuple:
        return (None, None)

    async def get_token_account_balance(self, *_a: object) -> int:
        return self.balance_raw


def _implementations() -> SimpleNamespace:
    async def build(*_a: object) -> list:
        return []

    return SimpleNamespace(
        address_provider=SimpleNamespace(
            derive_pool_address=lambda _m: CURVE,
            derive_creator_vault=lambda _c: CURVE,
        ),
        instruction_builder=SimpleNamespace(
            build_buy_instruction=build,
            get_required_accounts_for_buy=lambda *_a, **_k: [],
            get_buy_compute_unit_limit=lambda _o: 100_000,
        ),
        curve_manager=SimpleNamespace(),
    )


def _run_buy(client: StubBuyClient) -> TradeResult:
    wallet = SimpleNamespace(
        pubkey=CURVE,
        keypair=None,
        get_associated_token_address=lambda _m, _p: CURVE,
    )
    buyer = PlatformAwareBuyer(
        client,
        wallet,
        SimpleNamespace(calculate_priority_fee=_noop),
        amount=0.002,
        slippage=0.3,
        max_retries=1,
        extreme_fast_mode=True,
    )
    original = platform_aware.get_platform_implementations
    platform_aware.get_platform_implementations = lambda *_a: _implementations()
    try:
        return asyncio.run(buyer.execute(_token_info()))
    finally:
        platform_aware.get_platform_implementations = original


def check_polling_error_reads_back() -> bool:
    print("\n1. Status polling that raises still reads the transaction back")
    client = object.__new__(SolanaClient)
    client._rate_limiter = SimpleNamespace(acquire=_noop)  # noqa: SLF001

    async def confirm(*_a: object, **_k: object) -> None:
        raise _wrapped(httpx2.ReadTimeout("poll timed out"))

    async def get_client() -> SimpleNamespace:
        return SimpleNamespace(confirm_transaction=confirm)

    async def verify(_sig: object) -> ConfirmationStatus:
        return ConfirmationStatus.SUCCESS

    client.get_client = get_client
    client.verify_transaction_status = verify
    status = asyncio.run(client.confirm_transaction_detailed(SIGNATURE))
    return _check(
        "confirm_transaction_detailed",
        passed=status is ConfirmationStatus.SUCCESS,
        detail=f"{status.name} (want SUCCESS from the getTransaction read-back)",
    )


def check_recheck_finds_landed_buy() -> bool:
    print("\n2. An unconfirmed buy the re-check finds landed is a buy")
    result = _run_buy(StubBuyClient(ConfirmationStatus.SUCCESS, balance_raw=0))
    return _check("success", passed=bool(result.success), detail=str(result.success))


def check_wallet_balance_proves_landed_buy() -> bool:
    print("\n3. Invisible on chain but tokens in the wallet: a buy")
    result = _run_buy(StubBuyClient(UNCONFIRMED, balance_raw=69_745_000_000))
    return _check(
        "success / amount",
        passed=bool(result.success) and result.amount > 0,
        detail=f"{result.success} / {result.amount}",
    )


def check_unresolved_buy_keeps_signature() -> bool:
    print("\n4. Nothing on chain or in the wallet: UNCONFIRMED, signature kept")
    result = _run_buy(StubBuyClient(UNCONFIRMED, balance_raw=0))
    return _check(
        "success / reason / signature",
        passed=result.success is False
        and result.failure_reason is TradeFailureReason.UNCONFIRMED
        and result.tx_signature == SIGNATURE,
        detail=f"{result.success} / {result.failure_reason} / "
        f"{(result.tx_signature or '')[:8]}",
    )


def check_trader_never_burns_unresolved_buy() -> bool:
    print("\n5. No on_fail cleanup for an UNCONFIRMED buy; REVERTED still cleans")
    calls: list[object] = []

    async def cleanup(*args: object) -> None:
        calls.append(args)

    original = universal_trader.handle_cleanup_after_failure
    universal_trader.handle_cleanup_after_failure = cleanup
    try:
        trader = object.__new__(UniversalTrader)
        trader.traded_mints, trader.traded_token_programs = set(), {}
        for name in (
            "solana_client",
            "wallet",
            "priority_fee_manager",
            "cleanup_mode",
            "cleanup_with_priority_fee",
            "cleanup_force_close_with_burn",
        ):
            setattr(trader, name, None)
        info = _token_info()
        unresolved = TradeResult(
            success=False,
            tx_signature=SIGNATURE,
            failure_reason=TradeFailureReason.UNCONFIRMED,
        )
        asyncio.run(trader._handle_failed_buy(info, unresolved))  # noqa: SLF001
        after_unresolved = (len(calls), MINT in trader.traded_mints)
        reverted = TradeResult(
            success=False, failure_reason=TradeFailureReason.REVERTED
        )
        asyncio.run(trader._handle_failed_buy(info, reverted))  # noqa: SLF001
    finally:
        universal_trader.handle_cleanup_after_failure = original
    return _check(
        "cleanup calls after UNCONFIRMED / mint tracked / after REVERTED",
        passed=after_unresolved == (0, True) and len(calls) == 1,
        detail=f"{after_unresolved[0]} / {after_unresolved[1]} / {len(calls)} "
        f"(want 0 / True / 1)",
    )


def _send_error(inner: Exception) -> Exception:
    """Drive the real build_and_send_transaction into one failing send."""
    client = object.__new__(SolanaClient)
    client._rate_limiter = SimpleNamespace(acquire=_noop)  # noqa: SLF001

    async def send(*_a: object) -> None:
        raise _wrapped(inner)

    async def get_client() -> SimpleNamespace:
        return SimpleNamespace(send_transaction=send)

    async def blockhash() -> Hash:
        return Hash.default()

    client.get_client = get_client
    client.get_cached_blockhash = blockhash
    try:
        asyncio.run(client.build_and_send_transaction([], Keypair(), max_retries=1))
    except Exception as e:  # noqa: BLE001
        return e
    raise AssertionError("send did not raise")  # noqa: TRY003


def check_send_timeout_carries_signature() -> bool:
    print("\n6. A send that timed out carries its signature; a refused one not")
    timed_out = _send_error(httpx2.ReadTimeout("no reply"))
    refused = _send_error(httpx2.ConnectError("refused"))
    return _check(
        "signature on read timeout / on connect error",
        passed=getattr(timed_out, "tx_signature", None) is not None
        and getattr(refused, "tx_signature", None) is None,
        detail=f"{getattr(timed_out, 'tx_signature', None) is not None} / "
        f"{getattr(refused, 'tx_signature', None) is not None} (want True / False)",
    )


def _run_sell(error: Exception) -> TradeResult:
    async def raise_it(*_a: object, **_k: object) -> None:
        raise error

    async def state(*_a: object, **_k: object) -> tuple:
        return {"quote_mint": WSOL_MINT}, None

    async def build(*_a: object) -> list:
        return []

    impl = SimpleNamespace(
        address_provider=SimpleNamespace(
            derive_pool_address=lambda _m: CURVE,
            derive_creator_vault=lambda _c: CURVE,
        ),
        instruction_builder=SimpleNamespace(
            build_sell_instruction=build,
            get_required_accounts_for_sell=lambda *_a, **_k: [],
            get_sell_compute_unit_limit=lambda _o: 60_000,
        ),
        curve_manager=SimpleNamespace(get_pool_state_and_token_program=state),
    )
    client = SimpleNamespace(build_and_send_transaction=raise_it)
    seller = PlatformAwareSeller(
        client,
        SimpleNamespace(pubkey=CURVE, keypair=None),
        SimpleNamespace(calculate_priority_fee=_noop),
        slippage=0.3,
        max_retries=1,
    )
    original = platform_aware.get_platform_implementations
    platform_aware.get_platform_implementations = lambda *_a: impl
    try:
        return asyncio.run(seller.execute(_token_info(), 1000.0, 3e-8))
    finally:
        platform_aware.get_platform_implementations = original


def check_seller_reports_unknown_send() -> bool:
    print("\n7. Seller: timed-out send is UNCONFIRMED, refused send SUBMIT_FAILED")
    timed_out = _run_sell(_send_error(httpx2.ReadTimeout("no reply")))
    refused = _run_sell(_send_error(httpx2.ConnectError("refused")))
    return _check(
        "reason on read timeout / on connect error",
        passed=timed_out.failure_reason is TradeFailureReason.UNCONFIRMED
        and timed_out.tx_signature is not None
        and refused.failure_reason is TradeFailureReason.SUBMIT_FAILED,
        detail=f"{timed_out.failure_reason} / {refused.failure_reason}",
    )


def check_buyer_reports_unknown_send() -> bool:
    print("\n8. Buyer: a timed-out send is UNCONFIRMED with its signature")
    error = _send_error(httpx2.ReadTimeout("no reply"))
    result = _run_buy(StubBuyClient(UNCONFIRMED, balance_raw=0, send_error=error))
    return _check(
        "reason / signature",
        passed=result.failure_reason is TradeFailureReason.UNCONFIRMED
        and result.tx_signature is not None,
        detail=f"{result.failure_reason} / {result.tx_signature is not None}",
    )


def main() -> None:
    print("=" * 72)
    print("Verifying an unknown trade outcome is re-checked, never assumed failed")
    print("=" * 72)
    results = []
    for check in (
        check_polling_error_reads_back,
        check_recheck_finds_landed_buy,
        check_wallet_balance_proves_landed_buy,
        check_unresolved_buy_keeps_signature,
        check_trader_never_burns_unresolved_buy,
        check_send_timeout_carries_signature,
        check_seller_reports_unknown_send,
        check_buyer_reports_unknown_send,
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
