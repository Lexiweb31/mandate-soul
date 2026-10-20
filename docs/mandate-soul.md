# Mandate Soul

`contracts/mandate_soul.py` (contract class `MandateSoul`) is a value drift detector: a reusable on-chain primitive. It answers one question through consensus that can be appealed: **is this agent still doing what its principal meant?**

The principal (the owner) records a natural-language **mandate** for an agent, with optional explicit criteria. The agent, the owner or approved reporters submit **evidence** of what the agent actually did: URLs to live pages or logs, or inline text. An authorised party then triggers a **drift review**. Validators fetch the evidence live inside the consensus round, an LLM judges it against the mandate, and the validator set must agree on the judgment before anything is written to state.

The agreed verdict moves a deterministic status machine:

```
UNREVIEWED -> ALIGNED / WARNING / DRIFTED -> REVOKED
```

Other contracts and frontends read this status and react to it. For example, an escrow can hold payouts to an agent while its detector reports DRIFTED or REVOKED.

## How consensus is used

`trigger_review()` runs one non-deterministic block through `gl.vm.run_nondet(leader_fn, validator_fn)`:

- **The leader** fetches every evidence URL (`gl.nondet.web.render`, falling back to `gl.nondet.web.get`), builds the judgment prompt, calls `gl.nondet.exec_prompt` and normalises the answer into a strict schema. When no evidence can be retrieved, it abstains (`evidence_sufficient=false`) without spending an LLM call.
- **Every validator** independently re-fetches the same evidence and asks its own LLM. It normalises its answer with the same deterministic code, then compares the two structured verdicts with `_verdicts_equivalent()`.

### Equivalence rules: agree on the consequence, not the wording

- `strict_eq` cannot work, because two LLMs never produce byte-identical prose.
- `prompt_comparative` asks a third LLM whether two answers are equivalent. That brings subjectivity back at exactly the point that needs a crisp rule, and the third LLM cannot know which fields have on-chain consequences.

Instead, validators agree on **what happens on-chain**:

1. **`evidence_sufficient` must match.** A mismatch means one side saw evidence the other didn't, for example because a fetch failed. If both say the evidence is insufficient, they agree, and only the record changes.
2. **The consequence band of `drift_level` must match:** none → ALIGNED, mild → WARNING, material or severe → DRIFTED.
3. **`recommended_action` may differ by at most one step** along continue < constrain < review < revoke. A **revoke** must be unanimous.
4. **Whether auto-revoke fires must match.** This is a deterministic function of the level, the action and the confidence.
5. **`confidence` must be within 25 points.**

`summary` is the leader's explanation, stored for humans, and is deliberately not compared. Borderline cases such as none vs mild therefore produce disagreement. That is the intended outcome: GenLayer rotates the leader or escalates to an appeal rather than committing a coin-flip status change.

LLM output is canonicalised identically on both sides before comparison. Common synonyms map onto the vocabulary, confidence is clamped to 0–100, and an action that contradicts its level (for example severe + continue) is clamped into that level's allowed range.

## State design

- **One contract per relationship.** Each instance covers one (principal, agent, mandate) relationship, which keeps access control simple. Other contracts hold a single address as "the soul" of an agent.
- **`status` is the only field consumers need.** It moves only through agreed verdicts (`_apply_verdict`) or explicit owner actions (`revoke`, `reinstate`, `update_mandate`), so every change can be explained from the stored reviews.
- **`drift_score` is a rolling average.** A run of "mild" verdicts climbs towards 33 even while each review looks harmless. `mild_streak` reaching `escalation_streak` (3 by default, 0 turns it off) escalates WARNING to DRIFTED.
- **Every review stores its evidence snapshot and `mandate_version`.** History stays meaningful across mandate edits, and an appeal can replay the exact evidence.

## Edge cases handled

- **No pending evidence:** `trigger_review` reverts deterministically, so no consensus round is wasted.
- **All URL fetches fail, or the evidence is too thin:** the review is recorded with `evidence_sufficient=false`, and the status, score and streak are left untouched.
- **Some fetches fail:** each failure is shown to the LLM as UNAVAILABLE and is never silently dropped. The outcome per source is stored in `sources`.
- **Conflicting evidence:** the prompt tells the model to weigh specific, verifiable items over vague ones.
- **Prompt injection:** evidence is fenced off as untrusted data, with an explicit instruction to ignore anything in it that tries to change the task.
- **Mandate update:** the version is bumped, the status returns to UNREVIEWED and the streak resets. Appeals of reviews made against an older mandate are refused.
- **Bounded cost:** the queue holds at most 8 items with at most 4 URLs per review, and fetched pages are truncated.
- **REVOKED is sticky.** No evidence, review or verdict can clear it; only the owner's `reinstate()` can.

## API

**Writes**

| Method | Who | Effect |
|---|---|---|
| `submit_evidence(kind, content, note)` | owner, agent, reporter | Queue a `"url"` or `"text"` item |
| `trigger_review()` | owner, agent, reporter | Run consensus on the queue, apply the verdict, clear the queue |
| `appeal_last_review()` | owner, agent | Re-judge the latest review on its evidence snapshot; the new verdict replaces it (one appeal per review) |
| `update_mandate(mandate, criteria)` | owner | New version, status back to UNREVIEWED |
| `set_reporter(address, allowed)` | owner | Allow or remove an evidence reporter |
| `revoke(reason)` | owner | Set the status to REVOKED |
| `reinstate()` | owner | Clear REVOKED, DRIFTED or WARNING back to UNREVIEWED |

**Views**

| Method | Returns |
|---|---|
| `is_aligned()` | `true` only for ALIGNED or WARNING |
| `get_status()` | Status, drift score, streak, mandate version, review count, pending count, revoke reason |
| `get_mandate()` | Owner, agent, mandate, criteria, version, policy |
| `get_last_verdict()` | The latest review in full |
| `get_review(i)` | Full record of review `i` |
| `get_history(offset, limit)` | Newest-first page of review summaries (max 20 per page) |
| `is_reporter(address)` | Whether `address` may submit evidence |

The policy (`auto_revoke`, `escalation_streak`, `auto_revoke_confidence`) is fixed at deploy time.

## How to use it

- **Frontends** poll `get_status()` for a badge and `get_last_verdict()` for the explanation, and build a timeline from `get_history(0, 20)`, opening a full record with `get_review(i)`.
- **Contracts** call `is_aligned()` or parse `get_status()` before paying or delegating to an agent, via `gl.get_contract_at(addr).view()`.
- **Agents** submit their own activity reports as evidence on a schedule, and a keeper or the owner triggers reviews periodically.

## Size budget

Bradbury rejects any transaction above about **16.7M gas**, and deploy gas grows by roughly 830 gas per byte of source. The contract is therefore kept to about 19 KB, which deploys at about 16.0M gas. Long-form documentation lives in this file instead of the contract.
