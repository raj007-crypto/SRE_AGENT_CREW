from agents.approval_gate import notify_human
from data.fake_data_store import build_alert
from incident_schema import Incident, RemediationProposal


def _incident() -> Incident:
    return Incident(
        id="s1",
        alert=build_alert("bad_deploy"),
        proposal=RemediationProposal(
            action="rollback",
            target="a1b2c3d",
            justification="roll the bad deploy back",
        ),
    )


def test_missing_slack_channel_does_not_crash(monkeypatch, capsys):
    # regression test for the KeyError crash: token set, channel missing
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("SLACK_APPROVAL_CHANNEL", raising=False)

    notify_human(_incident())  # must not raise mid-graph

    out = capsys.readouterr().out
    assert "SLACK_APPROVAL_CHANNEL" in out


def test_no_slack_config_falls_back_to_print(monkeypatch, capsys):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APPROVAL_CHANNEL", raising=False)

    notify_human(_incident())

    out = capsys.readouterr().out
    assert "approval_gate:MOCK" in out
