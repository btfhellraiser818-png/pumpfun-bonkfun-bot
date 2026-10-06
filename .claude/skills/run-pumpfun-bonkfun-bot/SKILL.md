---
name: run-pumpfun-bonkfun-bot
description: Run, start, test and drive the pump.fun / letsbonk.fun sniper bot. Use when asked to run the bot, watch it detect live coins, validate a bot config, run the regression verifiers, or check a change without spending funds.
---

A Python sniper that listens for new pump.fun coins, buys at launch and sells
on an exit strategy. Drive it with `.claude/skills/run-pumpfun-bonkfun-bot/driver.py`:
it runs the offline verifiers, validates configs, and runs a real mainnet
listener that prints every coin it sees. **None of its subcommands builds or
sends a transaction.** Running the bot itself trades real SOL; see the human path.

All paths are relative to the repo root.

## Setup

```bash
uv sync
uv pip install -e .
```

Endpoints and the wallet key come from exported variables; this checkout has no
`.env`. Only `listen` needs one, and only the WSS URL:

```bash
export SOLANA_NODE_WSS_ENDPOINT='wss://...'   # provider URL incl. its api key
```

## Run (agent path)

```bash
# Offline: every regression verifier, or named ones. No network, no funds.
uv run .claude/skills/run-pumpfun-bonkfun-bot/driver.py verify
uv run .claude/skills/run-pumpfun-bonkfun-bot/driver.py verify verify_skip_mayhem_mode.py

# Offline: load + validate every bots/*.yaml the way bot_runner does.
uv run .claude/skills/run-pumpfun-bonkfun-bot/driver.py config

# Live, read-only: the bot's own logs listener against mainnet.
uv run .claude/skills/run-pumpfun-bonkfun-bot/driver.py listen --count 2 --timeout 90
```

`listen` output (exit 0 when at least one coin arrived, 1 on timeout):

```
LISTENING listener=logs platform=pump_fun
COIN 1 +  3.8s 'PSOKLITVWLOF' mint=CvoA... creator=8twA... mayhem=True quote=So111... state_from_event=True
DONE coins=2 in 8.0s
```

| subcommand | what it does |
|---|---|
| `verify [script ...]` | `tests/regression/run_all.py`; expect `37/37 passed` |
| `config [yaml ...]` | validates configs; fills unset `${VAR}`s with placeholders and never reads `.env` |
| `listen --listener logs\|blocks --count N --timeout S` | prints each detected coin: mint, creator, mayhem flag, quote mint, `state_from_event` |

`listen` is how to check a listener, event-parser or filter change against live
traffic: `mayhem=` and `state_from_event=` are the fields the buy path and the
`skip_mayhem_mode` filter read.

## Run (human path) — real funds

```bash
uv run src/bot_runner.py     # every enabled bots/*.yaml; trades real SOL
```

Needs `SOLANA_NODE_RPC_ENDPOINT`, `SOLANA_NODE_WSS_ENDPOINT` and
`SOLANA_PRIVATE_KEY` exported. Only with the user's explicit approval for the
session, after stating what runs and what it can cost (CLAUDE.md). It writes
`logs/<bot>_<timestamp>.log`; check that the file appeared, since a run where
every bot is disabled prints nothing and exits 0. `bots/bot-sniper-2-logs.yaml`
ships `enabled: true`.

## Gotchas

- **`blocks` is dead on Helius.** `blockSubscribe` answers
  `{'code': -32601, 'message': 'Method not found'}`; the listener logs it once
  and then waits forever with no coins. Use `listen --listener logs` (the
  default) on this provider.
- **`config_loader` loads the YAML's `env_file` with `override=True`.** A `.env`
  beside the bot would silently replace exported endpoints and key. The driver
  stubs that load out.
- **`tools/simulate_bot_buy_path.py` needs a Geyser endpoint.** Without
  `GEYSER_ENDPOINT` it dies at once with `KeyError: 'GEYSER_ENDPOINT'`; there is
  no logs-listener mode.
- **`listen` exits up to ~10 s after its last coin.** Shutting the logs
  listener down waits on its WebSocket closing; `DONE` time includes that.
- **Mayhem coins are common.** The first coin `listen` printed here was
  `mayhem=True`. Their sells can revert with Overflow (6024) at any slippage;
  `filters.skip_mayhem_mode: true` skips them.

## Troubleshooting

- **`RuntimeError: No cached blockhash available yet` from
  `tools/cleanup_accounts.py`**: the tool sends before `SolanaClient`'s first
  blockhash fetch lands. Wrap it so the read waits (this burns and closes a real
  token account; needs `SOLANA_NODE_RPC_ENDPOINT` and `SOLANA_PRIVATE_KEY`):

  ```bash
  uv run python - <MINT> <<'PY'
  import asyncio, sys
  sys.path.insert(0, "src"); sys.path.insert(0, "tools")
  from solders.pubkey import Pubkey
  from core.client import SolanaClient
  orig = SolanaClient.get_cached_blockhash
  async def patient(self):
      for _ in range(60):
          try:
              return await orig(self)
          except RuntimeError:
              await asyncio.sleep(0.5)
      return await orig(self)
  SolanaClient.get_cached_blockhash = patient
  import cleanup_accounts
  asyncio.run(cleanup_accounts.cleanup(Pubkey.from_string(sys.argv[1])))
  PY
  ```
