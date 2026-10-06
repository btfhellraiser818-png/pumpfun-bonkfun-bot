"""Verify the logs parser takes the base mint's token program from CreateEvent.

The parser hardcoded Token-2022 for every CreateEvent, although the event names
the program in its `token_program` field. A legacy `create` coin is SPL Token,
so its ATA and associated bonding curve were derived under the wrong program.
That TokenInfo is marked `state_from_event`, so extreme_fast_mode trusts it
without the curve read that would correct it, and the buy reverts.

Offline machine checks, no network and no funds moved. The legacy case is the
recorded mayhem create with its `token_program` bytes swapped for SPL Token, so
the rest of the event is real:

  1. A create_v2 event still parses to Token-2022.
  2. The same event naming SPL Token parses to SPL Token.
  3. Its associated bonding curve is derived under SPL Token.

Usage:
    uv run tests/regression/verify_event_token_program.py
"""

import base64
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.pubkeys import SystemAddresses  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddressProvider  # noqa: E402
from platforms.pumpfun.event_parser import PumpFunEventParser  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

FIXTURE = (
    PROJECT_ROOT
    / "cookbook"
    / "pumpfun"
    / "decode"
    / "raw_create_v2_mayhem_from_gettransaction.json"
)
DATA_PREFIX = "Program data: "


def _check(label: str, *, passed: bool, detail: str) -> bool:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


def _parse(logs: list[str]) -> TokenInfo | None:
    parser = PumpFunEventParser(
        idl_parser=get_idl_manager().get_parser(Platform.PUMP_FUN)
    )
    return parser.parse_token_creation_from_logs(logs, signature="fixture")


def _with_legacy_program(logs: list[str]) -> list[str]:
    """The same logs, the CreateEvent's token_program set to SPL Token."""
    t22 = bytes(SystemAddresses.TOKEN_2022_PROGRAM)
    legacy = bytes(SystemAddresses.TOKEN_PROGRAM)
    out, swapped = [], 0
    for line in logs:
        rewritten = line
        if line.startswith(DATA_PREFIX):
            raw = base64.b64decode(line[len(DATA_PREFIX) :])
            if raw.count(t22) == 1:
                swapped += 1
                rewritten = (
                    DATA_PREFIX + base64.b64encode(raw.replace(t22, legacy)).decode()
                )
        out.append(rewritten)
    if swapped == 0:
        raise SystemExit("fixture CreateEvent does not name Token-2022")  # noqa: TRY003
    return out


def main() -> None:
    print("=" * 72)
    print("Verifying CreateEvent's token_program reaches TokenInfo")
    print("=" * 72)
    logs = json.loads(FIXTURE.read_text())["meta"]["logMessages"]
    v2 = _parse(logs)
    legacy = _parse(_with_legacy_program(logs))
    provider = PumpFunAddressProvider()

    print("\n1. A create_v2 event parses to Token-2022")
    r1 = _check(
        "token_program_id",
        passed=bool(v2) and v2.token_program_id == SystemAddresses.TOKEN_2022_PROGRAM,
        detail=str(v2 and v2.token_program_id),
    )
    print("\n2. The event naming SPL Token parses to SPL Token")
    r2 = _check(
        "token_program_id",
        passed=bool(legacy)
        and legacy.token_program_id == SystemAddresses.TOKEN_PROGRAM,
        detail=str(legacy and legacy.token_program_id),
    )
    print("\n3. Its associated bonding curve is derived under SPL Token")
    want = (
        provider.derive_associated_bonding_curve(
            legacy.mint, legacy.bonding_curve, SystemAddresses.TOKEN_PROGRAM
        )
        if legacy
        else None
    )
    r3 = _check(
        "associated_bonding_curve",
        passed=bool(legacy) and legacy.associated_bonding_curve == want,
        detail="matches the SPL Token derivation"
        if legacy and legacy.associated_bonding_curve == want
        else str(legacy and legacy.associated_bonding_curve),
    )

    results = [r1, r2, r3]
    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
