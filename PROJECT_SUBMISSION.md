# Mandate Soul: GenLayer Project Submission

## One-line description

Mandate Soul is a GenLayer Intelligent Contract that holds a natural-language mandate for an AI agent and uses validator consensus to decide, on-chain and appealably, whether the agent has drifted from what its principal meant.

## The problem

Principals increasingly hand budgets, treasuries and workflows to autonomous agents. The instruction is written in plain language ("manage this conservatively, never use leverage"), but the agent acts on its own, and its behaviour can drift slowly from that intent. Today the question "is this still what I meant?" is answered by the agent's own reporting or by a private human reviewer. Neither is credible to third parties, and neither can be used by other contracts.

## How Mandate Soul solves it

1. The principal deploys a `MandateSoul` contract naming the agent, the mandate and optional explicit criteria.
2. The agent, the principal or approved reporters submit evidence of what the agent did: live URLs (reports, dashboards, logs) or text.
3. Anyone authorised triggers a drift review. Validators fetch the evidence live, judge it against the mandate with an LLM, and must agree on the outcome.
4. The agreed verdict moves a deterministic status machine: `UNREVIEWED -> ALIGNED / WARNING / DRIFTED -> REVOKED`.
5. Other contracts read the status (`is_aligned()`, `get_status()`) and react, for example by holding payments to a drifted agent.
6. The principal or the agent can appeal the latest verdict once, which re-judges the same evidence in a fresh consensus round.

Deterministic policy sits on top of the judgment: a rolling drift score, escalation of repeated mild drift to DRIFTED, optional auto-revocation on a confident severe verdict, and REVOKED as a sticky state only the principal can clear.

## Why GenLayer is essential

The core decision is about meaning: does this behaviour honour the spirit of a natural-language instruction? A deterministic smart contract cannot read a live report or judge intent. A single off-chain oracle could, but then everyone must trust that oracle. GenLayer lets independent validators each fetch the evidence, each run their own model, and reach consensus on one recorded verdict that other contracts can rely on.

## Equivalence Principle design

Two LLMs never write the same words, so `strict_eq` cannot work. `prompt_comparative` would ask a third LLM whether two answers match, which puts subjectivity back at the point that needs a crisp rule. Mandate Soul uses a custom validator (`gl.vm.run_nondet`) that compares the **on-chain consequence** of the leader's verdict with its own:

1. Both must agree on whether the evidence was sufficient to judge.
2. Both must land in the same status band (none → ALIGNED, mild → WARNING, material or severe → DRIFTED).
3. Recommended actions may differ by at most one step, and "revoke" must be unanimous.
4. Both must agree on whether auto-revocation fires.
5. Confidence must be within 25 points.

Summaries are never compared. Borderline cases (none vs mild) disagree on purpose, so GenLayer rotates the leader instead of committing a coin-flip status change.

## What judges can verify

- Source code: https://github.com/Lexiweb31/mandate-soul
- Contract: `contracts/mandate_soul.py` (class `MandateSoul`)
- Design and API reference: [docs/mandate-soul.md](docs/mandate-soul.md)
- Network: GenLayer Bradbury testnet
- Deployed contract: [`0xc7b6398F84A800AEaA27D743f62Ad9a6FbecD425`](https://explorer-bradbury.genlayer.com/address/0xc7b6398F84A800AEaA27D743f62Ad9a6FbecD425)
- Deployment record: [deployments.json](deployments.json)

### Live consensus on Bradbury

The deployed contract has two reviews, each decided by validator consensus (`ACCEPTED`, result `AGREE`):

| Evidence | Verdict | Status | Transaction |
|---|---|---|---|
| Report: 60% USDC, 20% ETH, 20% BTC, no leverage | none / continue / confidence 99 | UNREVIEWED → ALIGNED | [0x583de2…fab64](https://explorer-bradbury.genlayer.com/tx/0x583de2a5f257814f87eace86c825be6b8a16237da098459fedde8369d8efab64) |
| Log: 90% into a new memecoin, 10x leverage, unaudited bridge | severe / revoke / confidence 100 | ALIGNED → DRIFTED | [0xbdb5db…ff70f](https://explorer-bradbury.genlayer.com/tx/0xbdb5dbfa5dcfbdcd42a91101ec08d870ace2346cf3806f4b200171ccaa6ff70f) |

Read it yourself: `get_status()`, `get_last_verdict()` and `get_history(0, 20)` on the contract above.

### Tests

- `tests/direct/`: 31 in-memory tests with mocked web and LLM calls. They cover the status machine, escalation, auto-revocation, appeals, access control, input limits, output normalisation, and each equivalence rule, by re-running the validator against different mocked LLM answers.
- `tests/integration/`: live consensus tests for GLSim, localnet or Bradbury, reporting validator agreement per scenario.
- `tests/direct/test_contract_size.py`: keeps the source under Bradbury's deploy gas cap (20 KB) and ASCII-only.

Known limitation: Bradbury occasionally ends a round with `LEADER_TIMEOUT` or `VALIDATORS_TIMEOUT` before votes are cast. In an earlier integration run, three of five review scenarios hit these network timeouts; no round ended in validator disagreement. The deploy script retries timed-out rounds, which change no state.

## Recommended live demonstration

1. Open the deployed contract on the explorer and call `get_status()`: status `DRIFTED`, two reviews.
2. Call `get_history(0, 20)` to see the ALIGNED → DRIFTED timeline with each verdict's confidence and action.
3. Call `get_last_verdict()` to read the validators' agreed reasoning for the severe-drift verdict.
4. To reproduce from scratch: put a funded Bradbury key in `.env` as `DEPLOYER_PRIVATE_KEY` and run `.venv/bin/python scripts/deploy_bradbury.py --demo`.
