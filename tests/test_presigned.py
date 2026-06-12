"""presigned ハンドラの不変条件。

重点：
- ファイル名検証（.pdf 限定・basename でパストラバーサル防止・400 バイト上限）
- body=None / 不正 JSON → 400（CORS 無し 502 を避ける）
- _fast_ready の per-doc 判定（記録なし→not ready で初回 polling 偽陽性を防ぐ）
"""
import json
from unittest import mock


# ---------- ファイル名検証 ----------

def test_rejects_non_pdf(presigned_h):
    resp = presigned_h.create_presigned({"body": json.dumps({"filename": "evil.exe"})})
    assert resp["statusCode"] == 400


def test_rejects_empty_filename(presigned_h):
    resp = presigned_h.create_presigned({"body": json.dumps({"filename": ""})})
    assert resp["statusCode"] == 400


def test_rejects_too_long_filename(presigned_h):
    long_name = "あ" * 200 + ".pdf"  # UTF-8 で 600 バイト超
    resp = presigned_h.create_presigned({"body": json.dumps({"filename": long_name})})
    assert resp["statusCode"] == 400


def test_rejects_none_body(presigned_h):
    # API GW プロキシ統合は空ボディ POST で body=None を渡す → 400（502 にしない）
    resp = presigned_h.create_presigned({"body": None})
    assert resp["statusCode"] == 400


def test_rejects_malformed_json(presigned_h):
    resp = presigned_h.create_presigned({"body": "{not json"})
    assert resp["statusCode"] == 400


def test_ok_returns_signed_url(presigned_h, monkeypatch):
    monkeypatch.setattr(presigned_h.s3_client, "generate_presigned_url",
                        lambda *a, **k: "https://signed.example")
    monkeypatch.setattr(presigned_h, "_clear_ready_flags", lambda n: None)
    monkeypatch.setattr(presigned_h, "BUCKET_NAME", "bucket")
    resp = presigned_h.create_presigned({"body": json.dumps({"filename": "doc.pdf"})})
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["upload_url"] == "https://signed.example"
    assert body["key"] == "documents/doc.pdf"


def test_basename_blocks_path_traversal(presigned_h, monkeypatch):
    monkeypatch.setattr(presigned_h.s3_client, "generate_presigned_url",
                        lambda *a, **k: "https://signed.example")
    monkeypatch.setattr(presigned_h, "_clear_ready_flags", lambda n: None)
    monkeypatch.setattr(presigned_h, "BUCKET_NAME", "bucket")
    resp = presigned_h.create_presigned(
        {"body": json.dumps({"filename": "../../etc/passwd.pdf"})})
    body = json.loads(resp["body"])
    assert body["key"] == "documents/passwd.pdf"  # ディレクトリ要素が落ちる


# ---------- _fast_ready（per-doc 判定） ----------

def test_fast_ready_no_record_is_not_ready(presigned_h, monkeypatch):
    monkeypatch.setattr(presigned_h, "KNOWLEDGE_BASE_ID", "kb")
    monkeypatch.setattr(presigned_h, "DATA_SOURCE_ID", "ds")
    monkeypatch.setattr(presigned_h, "PDF_INDEXES_TABLE", "t")
    table = mock.MagicMock()
    table.get_item.return_value = {}  # この文書の #fast 記録なし
    monkeypatch.setattr(presigned_h.dynamodb, "Table", lambda n: table)
    # 記録がない＝ジョブ未起動 → not ready（初回 polling が前回ジョブの COMPLETE を拾わない）
    assert presigned_h._fast_ready("doc.pdf") == {"ready": False}


def test_fast_ready_complete_job_is_ready(presigned_h, monkeypatch):
    monkeypatch.setattr(presigned_h, "KNOWLEDGE_BASE_ID", "kb")
    monkeypatch.setattr(presigned_h, "DATA_SOURCE_ID", "ds")
    monkeypatch.setattr(presigned_h, "PDF_INDEXES_TABLE", "t")
    table = mock.MagicMock()
    table.get_item.return_value = {"Item": {"job_id": "J1"}}
    monkeypatch.setattr(presigned_h.dynamodb, "Table", lambda n: table)
    bedrock = mock.MagicMock()
    bedrock.list_ingestion_jobs.return_value = {
        "ingestionJobSummaries": [{"ingestionJobId": "J1", "status": "COMPLETE"}]}
    monkeypatch.setattr(presigned_h, "bedrock_agent", bedrock)
    assert presigned_h._fast_ready("doc.pdf") == {"ready": True}


def test_fast_ready_inprogress_job_not_ready(presigned_h, monkeypatch):
    monkeypatch.setattr(presigned_h, "KNOWLEDGE_BASE_ID", "kb")
    monkeypatch.setattr(presigned_h, "DATA_SOURCE_ID", "ds")
    monkeypatch.setattr(presigned_h, "PDF_INDEXES_TABLE", "t")
    table = mock.MagicMock()
    table.get_item.return_value = {"Item": {"job_id": "J1"}}
    monkeypatch.setattr(presigned_h.dynamodb, "Table", lambda n: table)
    bedrock = mock.MagicMock()
    bedrock.list_ingestion_jobs.return_value = {
        "ingestionJobSummaries": [{"ingestionJobId": "J1", "status": "IN_PROGRESS"}]}
    monkeypatch.setattr(presigned_h, "bedrock_agent", bedrock)
    assert presigned_h._fast_ready("doc.pdf") == {"ready": False}
