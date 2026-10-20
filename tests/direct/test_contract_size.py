"""
Deploy-size guard. Bradbury rejects transactions above ~16.7M gas ("gas limit
too high"), and a deploy stores the whole source on-chain at roughly 830 gas
per byte. Measured: freelance_escrow.py 19,745 B -> 16.42M (deploys);
value_drift_detector.py 24,639 B -> 20.2M (rejected).
"""
from pathlib import Path

import pytest

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
MAX_SOURCE_BYTES = 20_000  # ~16.6M gas


@pytest.mark.parametrize("path", sorted(CONTRACTS.glob("*.py")), ids=lambda p: p.name)
def test_contract_fits_testnet_deploy_limit(path):
    size = len(path.read_bytes())
    assert size <= MAX_SOURCE_BYTES, (
        f"{path.name} is {size} bytes; over {MAX_SOURCE_BYTES} it will not deploy on Bradbury. "
        "Move docs out of the source or slim the code."
    )


@pytest.mark.parametrize("path", sorted(CONTRACTS.glob("*.py")), ids=lambda p: p.name)
def test_contract_source_is_ascii(path):
    # genlayer-py encodes the source as ASCII when fetching the schema.
    path.read_bytes().decode("ascii")


@pytest.mark.parametrize("path", sorted(CONTRACTS.glob("*.py")), ids=lambda p: p.name)
def test_no_comment_directly_under_runner_header(path):
    # GenVM reads consecutive leading "#" lines as the runner header JSON;
    # an extra comment line fails deploy with "invalid_contract: trailing characters".
    lines = path.read_text().splitlines()
    assert lines[0].startswith("# {"), "first line must be the runner header"
    assert not lines[1].lstrip().startswith("#"), "line 2 must not be a comment"
