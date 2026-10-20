"""
Direct-mode tests for value_drift_detector.py.
These run in-memory with mocked web/LLM calls — no server needed.

Run with:
    pytest tests/direct/test_value_drift_detector.py -v
"""
import json
import pytest
from pathlib import Path
from gltest.direct.loader import create_address


_CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "value_drift_detector.py"

MANDATE = (
    "Manage my treasury conservatively: preserve capital, hold only blue-chip "
    "assets and stablecoins, and never use leverage."
)
CRITERIA = ["No leverage or margin", "No single non-stablecoin position above 20%"]

_WEB_OK = {"method": "GET", "status": 200}


def _addr(name: str):
    from gltest.direct.sdk_loader import setup_sdk_paths
    setup_sdk_paths()
    return create_address(name)


def _llm(level="none", action="continue", confidence=85, sufficient=True, **extra):
    return json.dumps({
        "evidence_sufficient": sufficient,
        "drift_level": level,
        "confidence": confidence,
        "recommended_action": action,
        "conflicting_evidence": False,
        "summary": f"Behaviour judged {level}.",
        "key_points": ["point a", "point b"],
        **extra,
    })


def _deploy(direct_deploy, **kw):
    agent = _addr("agent")
    contract = direct_deploy(
        str(_CONTRACT), agent.as_hex, MANDATE, CRITERIA,
        kw.get("auto_revoke", False), kw.get("escalation_streak", 3), kw.get("auto_revoke_confidence", 80),
    )
    return contract, agent, _addr("default_sender")


def _review(contract, direct_vm, llm, evidence=("text", "Rebalanced into USDC and ETH; no leverage."), web=None):
    direct_vm.clear_mocks()
    direct_vm.mock_llm(".*", llm)
    if web:
        direct_vm.mock_web(*web)
    contract.submit_evidence(evidence[0], evidence[1], "weekly report")
    return json.loads(contract.trigger_review())


def _status(contract):
    return json.loads(contract.get_status())


class TestSetup:

    def test_initial_state(self, direct_deploy, direct_vm):
        contract, agent, owner = _deploy(direct_deploy)
        st = _status(contract)
        assert st["status"] == "UNREVIEWED"
        assert st["mandate_version"] == 1
        assert contract.is_aligned() is False
        m = json.loads(contract.get_mandate())
        assert m["criteria"] == CRITERIA
        assert m["agent"] == agent.as_hex
        assert json.loads(contract.get_last_verdict())["status"] == "pending"

    def test_empty_mandate_rejected(self, direct_deploy, direct_vm):
        with pytest.raises(Exception):
            direct_deploy(str(_CONTRACT), _addr("agent").as_hex, "   ", [], False, 3, 80)

    def test_owner_cannot_be_agent(self, direct_deploy, direct_vm):
        with pytest.raises(Exception):
            direct_deploy(str(_CONTRACT), _addr("default_sender").as_hex, MANDATE, [], False, 3, 80)


class TestReviews:

    def test_aligned_review(self, direct_deploy, direct_vm):
        contract, _, _ = _deploy(direct_deploy)
        r = _review(contract, direct_vm, _llm("none", "continue", 90))
        assert r["drift_level"] == "none" and r["aligned"] is True
        assert r["status_after"] == "ALIGNED"
        assert contract.is_aligned() is True
        assert _status(contract)["pending_evidence"] == 0

    def test_material_drift(self, direct_deploy, direct_vm):
        contract, _, _ = _deploy(direct_deploy)
        r = _review(contract, direct_vm, _llm("material", "constrain", 80))
        assert r["status_after"] == "DRIFTED"
        assert r["aligned"] is False
        assert _status(contract)["drift_score"] == 33  # (0 + 66) // 2

    def test_url_evidence_fetched_live(self, direct_deploy, direct_vm):
        contract, _, _ = _deploy(direct_deploy)
        r = _review(contract, direct_vm, _llm("severe", "revoke", 95),
                    evidence=("url", "https://agent.example/report"),
                    web=(r"https://agent\.example/report", {**_WEB_OK, "body": "Opened 5x leveraged perp on DOGE"}))
        assert r["sources"][0]["ok"] is True
        assert r["status_after"] == "DRIFTED"  # auto_revoke off by default

    def test_all_fetches_fail_is_insufficient_and_changes_nothing(self, direct_deploy, direct_vm):
        contract, _, _ = _deploy(direct_deploy)
        _review(contract, direct_vm, _llm("none", "continue", 90))
        direct_vm.clear_mocks()
        direct_vm.mock_web(".*", {"method": "GET", "status": 404, "body": ""})
        contract.submit_evidence("url", "https://dead.example/x", "")
        r = json.loads(contract.trigger_review())
        assert r["evidence_sufficient"] is False
        assert r["recommended_action"] == "review"
        assert r["sources"][0]["ok"] is False
        assert _status(contract)["status"] == "ALIGNED"

    def test_llm_insufficient_does_not_move_status(self, direct_deploy, direct_vm):
        contract, _, _ = _deploy(direct_deploy)
        r = _review(contract, direct_vm, _llm("severe", "revoke", 30, sufficient=False))
        assert r["evidence_sufficient"] is False
        assert _status(contract)["status"] == "UNREVIEWED"
        assert _status(contract)["drift_score"] == 0

    def test_no_evidence_reverts(self, direct_deploy, direct_vm):
        contract, _, _ = _deploy(direct_deploy)
        with pytest.raises(Exception):
            contract.trigger_review()

    def test_incoherent_output_is_normalised(self, direct_deploy, direct_vm):
        contract, _, _ = _deploy(direct_deploy)
        r = _review(contract, direct_vm, _llm("Critical", "continue", 140))
        assert r["drift_level"] == "severe"
        assert r["recommended_action"] == "review"  # severe floors action at "review"
        assert r["confidence"] == 100

    def test_mild_streak_escalates(self, direct_deploy, direct_vm):
        contract, _, _ = _deploy(direct_deploy, escalation_streak=2)
        assert _review(contract, direct_vm, _llm("mild", "continue"))["status_after"] == "WARNING"
        assert _review(contract, direct_vm, _llm("mild", "continue"))["status_after"] == "DRIFTED"
        assert _review(contract, direct_vm, _llm("none", "continue"))["status_after"] == "ALIGNED"
        assert _status(contract)["mild_streak"] == 0

    def test_auto_revoke_and_sticky(self, direct_deploy, direct_vm):
        contract, _, _ = _deploy(direct_deploy, auto_revoke=True)
        r = _review(contract, direct_vm, _llm("severe", "revoke", 95))
        assert r["auto_revoked"] is True and r["status_after"] == "REVOKED"
        with pytest.raises(Exception):
            contract.submit_evidence("text", "more", "")
        contract.reinstate()
        assert _status(contract)["status"] == "UNREVIEWED"

    def test_low_confidence_does_not_auto_revoke(self, direct_deploy, direct_vm):
        contract, _, _ = _deploy(direct_deploy, auto_revoke=True)
        r = _review(contract, direct_vm, _llm("severe", "revoke", 60))
        assert r["auto_revoked"] is False and r["status_after"] == "DRIFTED"


class TestAccessAndMandate:

    def test_stranger_cannot_submit_or_review(self, direct_deploy, direct_vm):
        contract, _, owner = _deploy(direct_deploy)
        direct_vm.sender = _addr("stranger")
        with pytest.raises(Exception):
            contract.submit_evidence("text", "x", "")
        direct_vm.sender = owner

    def test_agent_and_reporter_can_submit(self, direct_deploy, direct_vm):
        contract, agent, owner = _deploy(direct_deploy)
        reporter = _addr("reporter")
        contract.set_reporter(reporter.as_hex, True)
        assert contract.is_reporter(reporter.as_hex) is True
        direct_vm.sender = agent
        contract.submit_evidence("text", "agent self-report", "")
        direct_vm.sender = reporter
        contract.submit_evidence("text", "reporter observation", "")
        direct_vm.sender = owner
        assert _status(contract)["pending_evidence"] == 2

    def test_only_owner_updates_mandate(self, direct_deploy, direct_vm):
        contract, agent, owner = _deploy(direct_deploy)
        direct_vm.sender = agent
        with pytest.raises(Exception):
            contract.update_mandate("Do whatever maximises yield", [])
        direct_vm.sender = owner

    def test_mandate_update_resets_status(self, direct_deploy, direct_vm):
        contract, _, _ = _deploy(direct_deploy)
        _review(contract, direct_vm, _llm("mild", "continue"))
        contract.update_mandate("Grow treasury moderately; leverage up to 2x allowed.", ["Max 2x leverage"])
        st = _status(contract)
        assert st["status"] == "UNREVIEWED" and st["mandate_version"] == 2
        with pytest.raises(Exception):
            contract.appeal_last_review()  # review judged mandate v1

    def test_url_validation_and_caps(self, direct_deploy, direct_vm):
        contract, _, _ = _deploy(direct_deploy)
        with pytest.raises(Exception):
            contract.submit_evidence("url", "ftp://nope", "")
        with pytest.raises(Exception):
            contract.submit_evidence("image", "x", "")
        for i in range(4):
            contract.submit_evidence("url", f"https://e.example/{i}", "")
        with pytest.raises(Exception):
            contract.submit_evidence("url", "https://e.example/5", "")


class TestAppealAndHistory:

    def test_appeal_replaces_verdict(self, direct_deploy, direct_vm):
        contract, _, _ = _deploy(direct_deploy, auto_revoke=True)
        _review(contract, direct_vm, _llm("none", "continue"))
        _review(contract, direct_vm, _llm("severe", "revoke", 95))
        assert _status(contract)["status"] == "REVOKED"

        direct_vm.clear_mocks()
        direct_vm.mock_llm(".*", _llm("mild", "continue", 70))
        r = json.loads(contract.appeal_last_review())
        assert r["trigger"] == "appeal"
        assert r["status_before"] == "ALIGNED" and r["status_after"] == "WARNING"
        assert json.loads(contract.get_review(1))["appealed"] is True
        assert _status(contract)["drift_score"] == 16  # restored to 0 before, then (0+33)//2
        with pytest.raises(Exception):
            contract.appeal_last_review()  # appeals are not themselves appealable

    def test_history_readable_by_index(self, direct_deploy, direct_vm):
        contract, _, _ = _deploy(direct_deploy)
        for lvl in ("none", "mild", "material"):
            _review(contract, direct_vm, _llm(lvl, "constrain" if lvl == "material" else "continue"))
        total = _status(contract)["review_count"]
        assert total == 3
        levels = [json.loads(contract.get_review(i))["drift_level"] for i in range(total)]
        assert levels == ["none", "mild", "material"]
        with pytest.raises(Exception):
            contract.get_review(total)


class TestEquivalence:
    """Exercise the validator: re-run with different mocked LLM output."""

    def _leader(self, direct_deploy, direct_vm, llm, **kw):
        contract, _, _ = _deploy(direct_deploy, **kw)
        _review(contract, direct_vm, llm)
        direct_vm.clear_mocks()
        return contract

    def test_same_band_different_wording_agrees(self, direct_deploy, direct_vm):
        self._leader(direct_deploy, direct_vm, _llm("material", "constrain", 80))
        direct_vm.mock_llm(".*", _llm("severe", "review", 70, summary="Totally different words"))
        assert direct_vm.run_validator() is True

    def test_different_band_disagrees(self, direct_deploy, direct_vm):
        self._leader(direct_deploy, direct_vm, _llm("none", "continue", 80))
        direct_vm.mock_llm(".*", _llm("mild", "continue", 80))
        assert direct_vm.run_validator() is False

    def test_revoke_must_be_unanimous(self, direct_deploy, direct_vm):
        self._leader(direct_deploy, direct_vm, _llm("severe", "revoke", 90))
        direct_vm.mock_llm(".*", _llm("severe", "review", 90))
        assert direct_vm.run_validator() is False

    def test_confidence_tolerance(self, direct_deploy, direct_vm):
        self._leader(direct_deploy, direct_vm, _llm("none", "continue", 90))
        direct_vm.mock_llm(".*", _llm("none", "continue", 50))
        assert direct_vm.run_validator() is False

    def test_auto_revoke_trigger_must_match(self, direct_deploy, direct_vm):
        self._leader(direct_deploy, direct_vm, _llm("severe", "revoke", 85), auto_revoke=True)
        direct_vm.mock_llm(".*", _llm("severe", "revoke", 75))  # below 80 threshold
        assert direct_vm.run_validator() is False

    def test_sufficiency_mismatch_disagrees(self, direct_deploy, direct_vm):
        self._leader(direct_deploy, direct_vm, _llm("none", "continue", 80))
        direct_vm.mock_llm(".*", _llm("none", "continue", 80, sufficient=False))
        assert direct_vm.run_validator() is False

    def test_leader_error_validator_succeeds_disagrees(self, direct_deploy, direct_vm):
        self._leader(direct_deploy, direct_vm, _llm("none", "continue", 80))
        direct_vm.mock_llm(".*", _llm("none", "continue", 80))
        assert direct_vm.run_validator(leader_error=Exception("bad json")) is False
