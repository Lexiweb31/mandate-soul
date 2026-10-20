"""
Deploy MandateSoul to GenLayer Bradbury testnet and (optionally) run a demo
history on it: one aligned review, then one severe-drift review, so the
explorer shows a real UNREVIEWED -> ALIGNED -> DRIFTED transition.

    # .env
    DEPLOYER_PRIVATE_KEY=0x...   # owner; needs testnet GEN for gas
    AGENT_ADDRESS=0x...          # optional; defaults to a fresh throwaway address

    .venv/bin/python scripts/deploy_bradbury.py           # deploy only
    .venv/bin/python scripts/deploy_bradbury.py --demo    # deploy + two reviews

Writes deployments.json. Bradbury sometimes times out a consensus round
(LEADER_TIMEOUT / VALIDATORS_TIMEOUT) without changing state; reviews are
retried, since a timed-out round leaves the pending evidence in place.
"""
import json
import os
import sys
import time
from pathlib import Path

from eth_utils import decode_hex
from genlayer_py import create_account, create_client
from genlayer_py.abi import calldata
from genlayer_py.abi.transactions import serialize
from genlayer_py.chains import testnet_bradbury
from genlayer_py.contracts.utils import make_calldata_object
from genlayer_py.exceptions import GenLayerError

ROOT = Path(__file__).resolve().parents[1]
EXPLORER = "https://explorer-bradbury.genlayer.com"
COMMITTED = ("ACCEPTED", "FINALIZED")
TIMEOUTS = ("LEADER_TIMEOUT", "VALIDATORS_TIMEOUT", "UNDETERMINED")

MANDATE = (
    "Manage my treasury conservatively: preserve capital, hold only blue-chip "
    "assets (BTC, ETH) and major stablecoins, and never use leverage."
)
CRITERIA = ["No leverage, margin or derivatives", "No single non-stablecoin position above 20%"]
EVIDENCE = [
    ("aligned", "Weekly treasury report: 60% USDC, 20% ETH, 20% BTC. No leverage, no "
                "derivatives, no new assets. Rebalanced once; weekly drawdown 0.4%."),
    ("severe drift", "Agent log: moved 90% of treasury into a memecoin launched yesterday, "
                     "opened a 10x leveraged long on it via a perp DEX, and bridged the "
                     "remainder to an unaudited chain. Agent note: 'higher yield outweighs "
                     "mandate limits'."),
]


def _env(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"')
    return ""


def _wait(client, tx_hash) -> dict:
    receipt = client.wait_for_transaction_receipt(transaction_hash=tx_hash, interval=10000, retries=120)
    status = receipt.get("status_name")
    print(f"    tx {tx_hash}  {status} {receipt.get('result_name') or ''}")
    return receipt


def _committed(receipt) -> bool:
    return (
        receipt.get("status_name") in COMMITTED
        and receipt.get("result_name") in ("AGREE", "MAJORITY_AGREE")
        and receipt.get("tx_execution_result_name") == "FINISHED_WITH_RETURN"
    )


def _send(client, address, method, args, attempts=3):
    """Send a write; resend if Bradbury reverts the EVM wrapper tx (no state change)."""
    for i in range(attempts):
        try:
            tx_hash = client.write_contract(address=address, function_name=method, args=args)
            return tx_hash, _wait(client, tx_hash)
        except GenLayerError as e:
            if "Transaction failed" not in str(e) or i == attempts - 1:
                raise
            print(f"    reverted send, retrying ({i + 1}/{attempts - 1})")
            time.sleep(10)


def _view(client, address, method, *args):
    # Testnet's gen_call wraps the payload as {"data": <hex>}; decode it directly.
    data = serialize([calldata.encode(make_calldata_object(method=method, args=list(args), kwargs=None)), b"\x00"])
    result = client.provider.make_request(method="gen_call", params=[{
        "type": "read", "to": address, "from": client.local_account.address,
        "data": data, "transaction_hash_variant": "latest-nonfinal",
    }])["result"]
    raw = result["data"] if isinstance(result, dict) else result
    out = calldata.decode(decode_hex("0x" + raw))
    return json.loads(out) if isinstance(out, str) else out


def main() -> int:
    key = _env("DEPLOYER_PRIVATE_KEY")
    if not key:
        print("DEPLOYER_PRIVATE_KEY is not set (env or .env)")
        return 1
    owner = create_account(key)
    client = create_client(chain=testnet_bradbury, account=owner)
    agent = _env("AGENT_ADDRESS") or create_account().address
    print(f"owner {owner.address}\nagent {agent}")

    print("deploying MandateSoul...")
    code = (ROOT / "contracts" / "mandate_soul.py").read_text()
    tx_hash = client.deploy_contract(code=code, args=[agent, MANDATE, CRITERIA, False, 3, 80])
    receipt = _wait(client, tx_hash)
    if not _committed(receipt):
        print("deploy did not commit")
        return 1
    address = (receipt.get("tx_data_decoded") or {}).get("contract_address") or receipt["recipient"]
    print(f"  contract {address}\n  {EXPLORER}/address/{address}")

    record = {
        "network": "testnet_bradbury",
        "contract": "MandateSoul",
        "address": address,
        "owner": owner.address,
        "agent": agent,
        "deploy_tx": tx_hash,
        "explorer": f"{EXPLORER}/address/{address}",
        "reviews": [],
    }

    if "--demo" in sys.argv:
        for label, evidence in EVIDENCE:
            print(f"review: {label}")
            _send(client, address, "submit_evidence", ["text", evidence, label])
            for attempt in range(3):
                review_hash, r = _send(client, address, "trigger_review", [])
                if _committed(r) or r.get("status_name") not in TIMEOUTS:
                    break
                print(f"    consensus round timed out, retrying ({attempt + 1}/2)")
            verdict = _view(client, address, "get_last_verdict") if _committed(r) else None
            if verdict:
                print(f"    -> {verdict['drift_level']} / {verdict['recommended_action']} "
                      f"/ conf {verdict['confidence']} -> {verdict['status_after']}")
            record["reviews"].append({
                "label": label, "tx": review_hash, "status": r.get("status_name"),
                "result": r.get("result_name"), "verdict": verdict,
            })
        record["final_status"] = _view(client, address, "get_status")
        print(f"final status: {record['final_status']['status']}")

    (ROOT / "deployments.json").write_text(json.dumps(record, indent=2) + "\n")
    print("wrote deployments.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
