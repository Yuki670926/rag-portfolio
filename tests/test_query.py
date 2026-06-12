"""query ハンドラの不変条件。

重点：
- CRITICAL 修正（user_id を claims.sub から取得し、無ければ 401 で fail-closed）
- RRF 融合の正しさ（両リストに出る文書が上位）
- dual のモード分岐とフォールバック（precise コールド→fast、opensearch 単独の障害→None=503）
"""
import json


# ---------- _rrf_merge（純ロジック） ----------

def test_rrf_merge_ranks_items_in_both_lists_highest(query_h):
    # _source の tag で識別する（_rrf_merge は _source のリストを返す）
    knn = [{"_id": "A", "_source": {"tag": "A"}},
           {"_id": "B", "_source": {"tag": "B"}},
           {"_id": "C", "_source": {"tag": "C"}}]
    bm25 = [{"_id": "B", "_source": {"tag": "B"}},
            {"_id": "A", "_source": {"tag": "A"}},
            {"_id": "D", "_source": {"tag": "D"}}]
    result = query_h._rrf_merge([knn, bm25], k=60, top=3)
    tags = [r["tag"] for r in result]
    # A・B は両リストに出るので C(knn のみ)・D(bm25 のみ)より上位
    assert set(tags[:2]) == {"A", "B"}
    assert len(result) == 3  # top で切られる


def test_rrf_merge_respects_top(query_h):
    knn = [{"_id": str(i), "_source": {"i": i}} for i in range(10)]
    result = query_h._rrf_merge([knn], k=60, top=3)
    assert len(result) == 3


def test_rrf_merge_empty(query_h):
    assert query_h._rrf_merge([[]], top=3) == []


# ---------- user_id fail-closed（CRITICAL 修正の固定） ----------

def test_handler_rejects_missing_claims(query_h, lambda_context):
    # Cognito authorizer の claims が無い（=user_id が取れない）→ 401 で拒否
    event = {"body": json.dumps({"question": "x"}),
             "requestContext": {"authorizer": {}}}
    resp = query_h.handler(event, lambda_context)
    assert resp["statusCode"] == 401


def test_handler_rejects_empty_question(query_h, lambda_context):
    event = {"body": json.dumps({"question": ""}),
             "requestContext": {"authorizer": {"claims": {"sub": "u1"}}}}
    resp = query_h.handler(event, lambda_context)
    assert resp["statusCode"] == 400


def test_handler_rejects_malformed_body(query_h, lambda_context):
    event = {"body": "{not json",
             "requestContext": {"authorizer": {"claims": {"sub": "u1"}}}}
    resp = query_h.handler(event, lambda_context)
    assert resp["statusCode"] == 400


def test_warmup_branch_skips_for_s3vectors(query_h, lambda_context, monkeypatch):
    # warmup イベントは認証不要・s3_vectors では暖機をスキップ（AWS を叩かない）
    monkeypatch.setattr(query_h, "VECTOR_STORE_TYPE", "s3_vectors")
    resp = query_h.handler({"warmup": True}, lambda_context)
    assert resp == {"warmup": "skipped"}


# ---------- search_documents モード分岐 ----------

def test_s3vectors_always_fast(query_h, monkeypatch):
    monkeypatch.setattr(query_h, "VECTOR_STORE_TYPE", "s3_vectors")
    monkeypatch.setattr(query_h, "_search_kb", lambda q: [{"text": "kb"}])
    ctx, mode, fb = query_h.search_documents("q", mode="precise")  # mode は無視される
    assert mode == "fast" and fb is False


def test_dual_precise_success(query_h, monkeypatch):
    monkeypatch.setattr(query_h, "VECTOR_STORE_TYPE", "dual")
    monkeypatch.setattr(query_h, "_search_opensearch_hybrid", lambda q: [{"text": "os"}])
    ctx, mode, fb = query_h.search_documents("q", mode="precise")
    assert mode == "precise" and fb is False


def test_dual_precise_cold_falls_back_to_fast(query_h, monkeypatch):
    monkeypatch.setattr(query_h, "VECTOR_STORE_TYPE", "dual")
    monkeypatch.setattr(query_h, "_search_opensearch_hybrid", lambda q: None)  # コールド/障害
    monkeypatch.setattr(query_h, "_search_kb", lambda q: [{"text": "kb"}])
    ctx, mode, fb = query_h.search_documents("q", mode="precise")
    assert mode == "fast" and fb is True


def test_dual_fast_mode_no_fallback_flag(query_h, monkeypatch):
    monkeypatch.setattr(query_h, "VECTOR_STORE_TYPE", "dual")
    monkeypatch.setattr(query_h, "_search_kb", lambda q: [{"text": "kb"}])
    ctx, mode, fb = query_h.search_documents("q", mode="fast")
    assert mode == "fast" and fb is False


def test_opensearch_only_failure_returns_none(query_h, monkeypatch):
    # opensearch 単独で障害(None)はフォールバック先が無い→None のまま（呼び出し側で 503）
    monkeypatch.setattr(query_h, "VECTOR_STORE_TYPE", "opensearch")
    monkeypatch.setattr(query_h, "_search_opensearch_hybrid", lambda q: None)
    ctx, mode, fb = query_h.search_documents("q", mode="precise")
    assert ctx is None
