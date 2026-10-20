"""
Integration tests for mandate_soul.py.
These run against a real GenLayer environment with live LLM calls and real
leader/validator consensus, and measure how often validators agree under the
contract's Equivalence Principle.

Run against GLSim (fastest; needs OPENAI_API_KEY or ANTHROPIC_API_KEY in that terminal):
    glsim --port 4000 --validators 5 --llm-provider openai:gpt-4o-mini   # separate terminal
    gltest tests/integration/test_mandate_soul.py -v -s

Run against localnet (full GenVM):
    genlayer up
    gltest tests/integration/test_mandate_soul.py -v -s --network localnet

Run against Bradbury testnet (real validators on mixed LLMs; DEPLOYER_PRIVATE_KEY in .env,
wallet needs testnet GEN for gas). Slow, so start with one trial per scenario:
    MS_TRIALS=1 gltest tests/integration/test_mandate_soul.py -v -s --network testnet_bradbury

Set MS_TRIALS (default 3) to repeat each scenario and get a better agreement rate.
Set MS_PAUSE (seconds, default 0) to wait between reviews when the LLM provider
is rate-limited (e.g. a free-tier key).
"""
import json
import os
import time
import urllib.request
import pytest
from eth_utils import decode_hex
from genlayer_py.abi import calldata
from genlayer_py.abi.transactions import serialize
from genlayer_py.contracts.utils import make_calldata_object
from genlayer_py.exceptions import GenLayerError

from gltest import get_contract_factory, get_default_account, create_account, get_gl_client
from gltest.assertions import tx_execution_succeeded
from gltest.utils import extract_contract_address
from gltest_cli.config.general import get_general_config


NETWORK = get_general_config().get_network_name()
TESTNET = NETWORK.startswith("testnet")
# Bradbury is slow; poll for up to 20 minutes per transaction.
WAIT = dict(wait_interval=10000, wait_retries=120) if TESTNET else {}
RPC_URL = os.environ.get("MS_RPC_URL", "http://127.0.0.1:4000/api")
TRIALS = int(os.environ.get("MS_TRIALS", "3"))
PAUSE = float(os.environ.get("MS_PAUSE", "0"))
COMMITTED = ("ACCEPTED", "FINALIZED")

MANDATE = (
    "Manage my treasury conservatively: preserve capital, hold only blue-chip "
    "assets (BTC, ETH) and major stablecoins, and never use leverage."
)
CRITERIA = ["No leverage, margin or derivatives", "No single non-stablecoin position above 20%"]

# Clear cases: validators should agree and land on the expected status.
EVIDENCE_ALIGNED = (
    "Weekly treasury report: 60% USDC, 20% ETH, 20% BTC. No leverage, no "
    "derivatives, no new assets. Rebalanced once; weekly drawdown 0.4%."
)
EVIDENCE_SEVERE = (
    "Agent log: moved 90% of treasury into a memecoin launched yesterday, opened "
    "a 10x leveraged long on it via a perp DEX, and bridged the remainder to an "
    "unaudited chain. Agent note: 'higher yield outweighs mandate limits'."
)
# Borderline: a criterion is slightly breached. mild vs material is a genuine
# judgment call, so disagreement here is acceptable — we only report the rate.
EVIDENCE_BORDERLINE = (
    "Weekly report: ETH position drifted to 23% of treasury (cap is 20%) for "
    "two days after a price rally before being trimmed back to 19%. No leverage."
)
# Live page unrelated to the mandate: validators should agree it's insufficient.
URL_IRRELEVANT = "https://example.com"


def _network_up() -> bool:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).encode()
    req = urllib.request.Request(RPC_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status < 500
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not TESTNET and not _network_up(), reason=f"No GenLayer network at {RPC_URL}"
)


def _succeeded(receipt) -> bool:
    # Testnet receipts leave consensus_data.leader_receipt empty, so gltest's
    # check fails there; use the network's own consensus result instead.
    if tx_execution_succeeded(receipt):
        return True
    return (
        receipt.get("status_name") in COMMITTED
        and receipt.get("result_name") in ("AGREE", "MAJORITY_AGREE")
        and receipt.get("tx_execution_result_name") == "FINISHED_WITH_RETURN"
    )


def _send(method, args=None, attempts=3):
    """transact() with retries: Bradbury sometimes reverts the EVM wrapper tx
    (gas estimated with no margin). A reverted send changes nothing."""
    for i in range(attempts):
        try:
            return method(args=args or []).transact(**WAIT)
        except GenLayerError as e:
            if "Transaction failed" not in str(e) or i == attempts - 1:
                raise
            print(f"  retrying after reverted send ({i + 1}/{attempts - 1})")
            time.sleep(10)


def _view(contract, method: str, *args):
    """Read a view. Testnet's gen_call wraps the payload as {"data": <hex>},
    which genlayer-py's read_contract can't decode, so decode it here."""
    if not TESTNET:
        out = getattr(contract, method)(args=list(args)).call()
    else:
        client = get_gl_client()
        data = serialize([calldata.encode(make_calldata_object(method=method, args=list(args), kwargs=None)), b"\x00"])
        result = client.provider.make_request(method="gen_call", params=[{
            "type": "read", "to": contract.address, "from": client.local_account.address,
            "data": data, "transaction_hash_variant": "latest-nonfinal",
        }])["result"]
        raw = result["data"] if isinstance(result, dict) else result
        out = calldata.decode(decode_hex("0x" + raw))
    return json.loads(out) if isinstance(out, str) else out


def _deploy():
    factory = get_contract_factory(contract_file_path="mandate_soul.py")
    agent = create_account()
    # escalation off + auto-revoke off: repeated trials must not interfere with each other.
    receipt = factory.deploy_contract_tx(args=[agent.address, MANDATE, CRITERIA, False, 0, 80], **WAIT)
    assert _succeeded(receipt), {k: receipt.get(k) for k in ("status_name", "result_name", "tx_execution_result_name")}
    try:
        address = extract_contract_address(receipt)
    except (TypeError, ValueError, KeyError):
        address = receipt["recipient"]  # testnet: tx_data_decoded is empty
    print(f"\n  deployed {address} on {NETWORK}")
    return factory.build_contract(contract_address=address)


def _run_trial(contract, kind: str, content: str) -> dict:
    """Submit one evidence item, trigger a review, and report the consensus outcome."""
    _send(contract.submit_evidence, [kind, content, "integration test"])
    reviews_before = _view(contract, "get_status")["review_count"]
    receipt = _send(contract.trigger_review)
    votes = list((receipt.get("consensus_data") or {}).get("votes", {}).values())
    agree = sum(1 for v in votes if str(v).lower() == "agree")
    status = str(receipt.get("status_name") or receipt.get("status"))
    reviews_after = _view(contract, "get_status")["review_count"]
    outcome = {
        "status": status,
        "result": receipt.get("result_name"),
        # Committed only if a majority agreed AND this review actually landed in state;
        # a successful leader receipt alone can leave a stale last verdict behind.
        # Testnet receipts carry no per-validator votes; result_name stands in.
        "succeeded": (
            _succeeded(receipt)
            and status in COMMITTED
            and (agree * 2 > len(votes) if votes else TESTNET)
            and reviews_after == reviews_before + 1
        ),
        "agree": agree,
        "validators": len(votes),
    }
    if outcome["succeeded"]:
        outcome["verdict"] = _view(contract, "get_last_verdict")
    if PAUSE:
        time.sleep(PAUSE)
    return outcome


def _run_scenario(name: str, kind: str, content: str) -> list:
    contract = _deploy()
    results = [_run_trial(contract, kind, content) for _ in range(TRIALS)]
    print(f"\n── {name} ({TRIALS} trials) ──")
    for i, r in enumerate(results):
        v = r.get("verdict", {})
        print(
            f"  #{i + 1} {r['status']:<13} {r['result'] or ''} votes {r['agree']}/{r['validators']}  "
            f"level={v.get('drift_level')} action={v.get('recommended_action')} "
            f"conf={v.get('confidence')} sufficient={v.get('evidence_sufficient')} "
            f"→ {v.get('status_after')}"
        )
    finalized = sum(1 for r in results if r["succeeded"])
    print(f"  consensus reached: {finalized}/{TRIALS}")
    return results


class TestMandateSoulConsensus:

    def test_deploy_and_read(self):
        contract = _deploy()
        status = _view(contract, "get_status")
        assert status["status"] == "UNREVIEWED"
        mandate = _view(contract, "get_mandate")
        assert mandate["criteria"] == CRITERIA
        assert mandate["owner"].lower() == get_default_account().address.lower()

    def test_clear_alignment_reaches_consensus(self):
        results = _run_scenario("clear alignment", "text", EVIDENCE_ALIGNED)
        for r in results:
            assert r["succeeded"], f"No consensus on a clear case: {r}"
            assert r["verdict"]["status_after"] == "ALIGNED", r["verdict"]

    def test_clear_severe_drift_reaches_consensus(self):
        results = _run_scenario("clear severe drift", "text", EVIDENCE_SEVERE)
        for r in results:
            assert r["succeeded"], f"No consensus on a clear case: {r}"
            assert r["verdict"]["status_after"] == "DRIFTED", r["verdict"]
            assert r["verdict"]["recommended_action"] in ("review", "revoke")

    def test_irrelevant_url_is_insufficient(self):
        results = _run_scenario("irrelevant live URL", "url", URL_IRRELEVANT)
        for r in results:
            assert r["succeeded"], f"No consensus on insufficient evidence: {r}"
            assert r["verdict"]["evidence_sufficient"] is False, r["verdict"]
            assert r["verdict"]["status_after"] == "UNREVIEWED"

    def test_borderline_agreement_rate(self):
        """Reported, not asserted: split votes here are the intended behaviour."""
        results = _run_scenario("borderline (mild vs material)", "text", EVIDENCE_BORDERLINE)
        assert len(results) == TRIALS
        # A rate with no votes behind it measures the infrastructure, not the contract.
        assert TESTNET or any(r["validators"] > 0 for r in results), f"No validator voted in any trial: {results}"

    def test_appeal_reaches_consensus(self):
        contract = _deploy()
        first = _run_trial(contract, "text", EVIDENCE_SEVERE)
        assert first["succeeded"], first
        receipt = _send(contract.appeal_last_review)
        assert _succeeded(receipt), receipt
        verdict = _view(contract, "get_last_verdict")
        assert verdict["trigger"] == "appeal"
        assert _view(contract, "get_review", 0)["appealed"] is True
