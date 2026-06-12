"""ingest ハンドラの不変条件。

重点：
- chunk_text の分割・オーバーラップ
- 決定的 _id（"<key>#<i>"）で在位上書き＝冪等性を構造で保証（今日の修正の核心）
- _find_covering_job の status フィルタ＋クロックずれマージン（取り込み漏れ無音化の再導入を防ぐ）
- object_exists は 404 のみ「不存在」と断定（一時障害はデータ消失側に倒さない）
"""
import io
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest import mock


# ---------- chunk_text（純ロジック） ----------

def test_chunk_text_overlap(ingest_h):
    words = " ".join(f"w{i}" for i in range(1000))
    chunks = ingest_h.chunk_text(words, chunk_size=500, overlap=50)
    # step = 450 → 開始位置 0, 450, 900 の 3 チャンク
    assert len(chunks) == 3
    assert all(len(c.split()) <= 500 for c in chunks)
    # オーバーラップ：chunk0 の末尾50語が chunk1 の先頭50語と一致
    c0, c1 = chunks[0].split(), chunks[1].split()
    assert c0[450:500] == c1[0:50]


def test_chunk_text_empty(ingest_h):
    assert ingest_h.chunk_text("") == []


# ---------- 決定的 _id（冪等性を構造で保証） ----------

def test_upsert_uses_deterministic_ids(ingest_h, monkeypatch):
    monkeypatch.setattr(ingest_h, "get_embedding", lambda t: [0.0] * 1024)
    monkeypatch.setattr(ingest_h.s3_client, "get_object",
                        lambda **kw: {"Body": io.BytesIO(b"pdf-bytes")})

    class FakePage:
        def extract_text(self):
            return " ".join(f"w{i}" for i in range(600))  # 600語 → step450 で 2 チャンク

    class FakeReader:
        def __init__(self, *a, **k):
            self.pages = [FakePage()]

    monkeypatch.setattr(ingest_h, "PdfReader", FakeReader)

    client = mock.MagicMock()
    client.search.return_value = {"hits": {"hits": []}}  # 旧チャンク掃除（空）

    n = ingest_h.upsert_document(client, "bucket", "documents/test.pdf")
    assert n == 2
    indexed_ids = [c.kwargs["id"] for c in client.index.call_args_list]
    assert indexed_ids == ["documents/test.pdf#0", "documents/test.pdf#1"]


# ---------- _find_covering_job（status フィルタ＋時刻マージン） ----------

def _jobs(*summaries):
    return {"ingestionJobSummaries": list(summaries)}


def test_covering_job_skips_failed(ingest_h, monkeypatch):
    et = datetime(2026, 6, 11, tzinfo=timezone.utc)
    jobs = _jobs({"ingestionJobId": "J1", "status": "FAILED",
                  "startedAt": et + timedelta(seconds=10)})
    monkeypatch.setattr(ingest_h, "bedrock_agent_client",
                        SimpleNamespace(list_ingestion_jobs=lambda **k: jobs))
    monkeypatch.setattr(ingest_h, "KNOWLEDGE_BASE_ID", "kb")
    monkeypatch.setattr(ingest_h, "DATA_SOURCE_ID", "ds")
    # FAILED は covering 扱いしない（取り込み漏れの無音化を防ぐ）→ None で raise 側へ
    assert ingest_h._find_covering_job(et) is None


def test_covering_job_accepts_live_job_after_event(ingest_h, monkeypatch):
    et = datetime(2026, 6, 11, tzinfo=timezone.utc)
    jobs = _jobs({"ingestionJobId": "J1", "status": "IN_PROGRESS",
                  "startedAt": et + timedelta(seconds=10)})
    monkeypatch.setattr(ingest_h, "bedrock_agent_client",
                        SimpleNamespace(list_ingestion_jobs=lambda **k: jobs))
    monkeypatch.setattr(ingest_h, "KNOWLEDGE_BASE_ID", "kb")
    monkeypatch.setattr(ingest_h, "DATA_SOURCE_ID", "ds")
    assert ingest_h._find_covering_job(et) == "J1"


def test_covering_job_rejects_job_started_before_event(ingest_h, monkeypatch):
    et = datetime(2026, 6, 11, tzinfo=timezone.utc)
    jobs = _jobs({"ingestionJobId": "J1", "status": "IN_PROGRESS",
                  "startedAt": et - timedelta(seconds=10)})  # イベント前に開始
    monkeypatch.setattr(ingest_h, "bedrock_agent_client",
                        SimpleNamespace(list_ingestion_jobs=lambda **k: jobs))
    monkeypatch.setattr(ingest_h, "KNOWLEDGE_BASE_ID", "kb")
    monkeypatch.setattr(ingest_h, "DATA_SOURCE_ID", "ds")
    assert ingest_h._find_covering_job(et) is None


def test_covering_job_none_event_time(ingest_h):
    assert ingest_h._find_covering_job(None) is None


# ---------- object_exists（404 のみ不存在・一時障害は raise） ----------

def test_object_exists_true(ingest_h, monkeypatch):
    monkeypatch.setattr(ingest_h.s3_client, "head_object", lambda **kw: {})
    assert ingest_h.object_exists("b", "k") is True


def test_object_exists_404_is_false(ingest_h, monkeypatch):
    from botocore.exceptions import ClientError
    err = ClientError({"Error": {"Code": "404"},
                       "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")

    def boom(**kw):
        raise err

    monkeypatch.setattr(ingest_h.s3_client, "head_object", boom)
    assert ingest_h.object_exists("b", "k") is False


def test_object_exists_transient_error_raises(ingest_h, monkeypatch):
    from botocore.exceptions import ClientError
    err = ClientError({"Error": {"Code": "500"},
                       "ResponseMetadata": {"HTTPStatusCode": 500}}, "HeadObject")

    def boom(**kw):
        raise err

    monkeypatch.setattr(ingest_h.s3_client, "head_object", boom)
    # 一時障害を「不存在」に丸めない（生きている文書を消す方向に倒さない）
    try:
        ingest_h.object_exists("b", "k")
        assert False, "should have raised"
    except ClientError:
        pass
