# Mandate Soul

*A value drift detector for AI agents, built as a GenLayer Intelligent Contract.*

It answers one question through validator consensus: **is this agent still doing what its principal meant?**

The owner records a natural-language mandate for an agent. The agent, the owner or approved reporters submit evidence (live URLs or text). A drift review fetches the evidence inside the consensus round, an LLM judges it against the mandate, and validators must agree on the on-chain consequence before the status changes:

```
UNREVIEWED -> ALIGNED / WARNING / DRIFTED -> REVOKED
```

Other contracts gate on `is_aligned()` or read `get_status()`.

Full design, equivalence rules and API: [docs/mandate-soul.md](docs/mandate-soul.md).

## Live on Bradbury testnet

[`0xc7b6398F84A800AEaA27D743f62Ad9a6FbecD425`](https://explorer-bradbury.genlayer.com/address/0xc7b6398F84A800AEaA27D743f62Ad9a6FbecD425): two consensus-decided reviews, UNREVIEWED → ALIGNED → DRIFTED. Details in [PROJECT_SUBMISSION.md](PROJECT_SUBMISSION.md) and [deployments.json](deployments.json).

## Layout

| Path | What |
|---|---|
| `contracts/mandate_soul.py` | The contract |
| `tests/direct/` | In-memory tests with mocked web/LLM (status machine, equivalence rules, appeals) |
| `tests/integration/` | Live consensus tests against GLSim, localnet or Bradbury testnet; reports validator agreement rates |

## Run

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/genvm-lint check contracts/mandate_soul.py
.venv/bin/pytest tests/direct -q
```

Live consensus (needs `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` in the GLSim terminal):

```bash
.venv/bin/glsim --port 4000 --validators 5 --llm-provider openai:gpt-4o-mini
.venv/bin/gltest tests/integration -v -s
```

To deploy on Bradbury, add a `testnet_bradbury` network with `accounts: [${DEPLOYER_PRIVATE_KEY}]` to `gltest.config.yaml` and put the key in `.env`. The source is kept under Bradbury's ~16.7M deploy gas cap.
