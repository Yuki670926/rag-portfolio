"""postauth（ログイン起点ウォーマー起動）ハンドラの不変条件。

重点：
- 認証フローを絶対にブロックしない（invoke 失敗でも event をそのまま返す）
- 起動失敗を CloudWatch EMF 形式のカスタムメトリクスとして記録する（改善#03）
  （Powertools レイヤを持たない最小構成のため手動 EMF）
"""
import json
from unittest import mock


def test_handler_returns_event_unchanged_on_success(postauth_h, monkeypatch):
    monkeypatch.setattr(postauth_h, "WARMER_TARGET", "rp-dev-query")
    invoke_mock = mock.MagicMock()
    monkeypatch.setattr(postauth_h.lambda_client, "invoke", invoke_mock)
    event = {"userName": "u1", "response": {}}
    result = postauth_h.handler(event, None)
    assert result == event
    invoke_mock.assert_called_once()


def test_handler_returns_event_unchanged_when_invoke_fails(postauth_h, monkeypatch):
    # 認証フローは失敗させてはならない：invoke が例外を投げても event をそのまま返す
    monkeypatch.setattr(postauth_h, "WARMER_TARGET", "rp-dev-query")
    monkeypatch.setattr(postauth_h.lambda_client, "invoke",
                         mock.MagicMock(side_effect=RuntimeError("boom")))
    event = {"userName": "u1", "response": {}}
    result = postauth_h.handler(event, None)
    assert result == event


def test_handler_skips_invoke_when_no_warmer_target(postauth_h, monkeypatch):
    monkeypatch.setattr(postauth_h, "WARMER_TARGET", "")
    invoke_mock = mock.MagicMock()
    monkeypatch.setattr(postauth_h.lambda_client, "invoke", invoke_mock)
    event = {"userName": "u1"}
    result = postauth_h.handler(event, None)
    assert result == event
    invoke_mock.assert_not_called()


# ---------- カスタムメトリクス：ウォーマー起動失敗を数える（改善#03） ----------

def test_handler_emits_emf_metric_when_invoke_fails(postauth_h, monkeypatch, capsys):
    monkeypatch.setattr(postauth_h, "WARMER_TARGET", "rp-dev-query")
    monkeypatch.setattr(postauth_h.lambda_client, "invoke",
                         mock.MagicMock(side_effect=RuntimeError("boom")))
    postauth_h.handler({"userName": "u1"}, None)

    out = capsys.readouterr().out
    emf_lines = [line for line in out.splitlines() if "WarmerInvokeFailure" in line]
    assert len(emf_lines) == 1
    payload = json.loads(emf_lines[0])
    assert payload["WarmerInvokeFailure"] == 1
    assert payload["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "RagPortfolio"
    assert payload["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]["Name"] == "WarmerInvokeFailure"


def test_handler_no_metric_when_invoke_succeeds(postauth_h, monkeypatch, capsys):
    monkeypatch.setattr(postauth_h, "WARMER_TARGET", "rp-dev-query")
    monkeypatch.setattr(postauth_h.lambda_client, "invoke", mock.MagicMock())
    postauth_h.handler({"userName": "u1"}, None)
    out = capsys.readouterr().out
    assert "WarmerInvokeFailure" not in out


def test_handler_no_metric_when_no_warmer_target(postauth_h, monkeypatch, capsys):
    monkeypatch.setattr(postauth_h, "WARMER_TARGET", "")
    postauth_h.handler({"userName": "u1"}, None)
    out = capsys.readouterr().out
    assert "WarmerInvokeFailure" not in out
