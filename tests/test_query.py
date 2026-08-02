"""query ハンドラの不変条件。

重点：
- CRITICAL 修正（user_id を claims.sub から取得し、無ければ 401 で fail-closed）
- RRF 融合の正しさ（両リストに出る文書が上位）
- dual のモード分岐とフォールバック（precise コールド→fast、opensearch 単独の障害→None=503）
"""
import json
from unittest import mock


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


# ---------- fast(KB) 経路の障害と正常0件の区別（無音障害の修正の固定） ----------

def test_search_kb_failure_returns_none_not_empty(query_h, monkeypatch):
    # KB Retrieve の例外は None（障害）。[] に丸めると「KB 停止中も 200 で
    # 『該当情報なし』」になり検索基盤の障害が無音化するため、規約を固定する
    class _Boom:
        def retrieve(self, **kw):
            raise RuntimeError("kb down")
    monkeypatch.setattr(query_h, "KNOWLEDGE_BASE_ID", "kb-test")
    monkeypatch.setattr(query_h, "bedrock_agent_runtime", _Boom())
    assert query_h._search_kb("q") is None


def test_search_kb_unconfigured_returns_none(query_h, monkeypatch):
    # ID 未設定は設定ミス＝障害扱い（文書0件と混同しない）
    monkeypatch.setattr(query_h, "KNOWLEDGE_BASE_ID", "")
    assert query_h._search_kb("q") is None


def test_search_kb_zero_hits_returns_empty_list(query_h, monkeypatch):
    # 正常応答で0件は [] のまま（「文書が無い」は障害ではない）
    class _Empty:
        def retrieve(self, **kw):
            return {"retrievalResults": []}
    monkeypatch.setattr(query_h, "KNOWLEDGE_BASE_ID", "kb-test")
    monkeypatch.setattr(query_h, "bedrock_agent_runtime", _Empty())
    assert query_h._search_kb("q") == []


def test_dual_both_paths_down_returns_none(query_h, monkeypatch):
    # dual で precise 障害→fast へフォールバック→fast も障害なら None が伝播（503）
    monkeypatch.setattr(query_h, "VECTOR_STORE_TYPE", "dual")
    monkeypatch.setattr(query_h, "_search_opensearch_hybrid", lambda q: None)
    monkeypatch.setattr(query_h, "_search_kb", lambda q: None)
    ctx, mode, fb = query_h.search_documents("q", mode="precise")
    assert ctx is None


def test_handler_returns_503_on_search_failure(query_h, lambda_context, monkeypatch):
    # 検索系の障害は 200＋「文書なし」ではなく 503 で返す（handler レベルの固定）
    monkeypatch.setattr(query_h, "get_session_id", lambda uid: "s1")
    monkeypatch.setattr(query_h, "get_conversation_history", lambda uid, sid: [])
    monkeypatch.setattr(query_h, "search_documents",
                        lambda q, mode: (None, "fast", False))
    event = {"body": json.dumps({"question": "x"}),
             "requestContext": {"authorizer": {"claims": {"sub": "u1"}}}}
    resp = query_h.handler(event, lambda_context)
    assert resp["statusCode"] == 503


# ---------- 500 応答の情報漏洩防止（内部詳細を返さない） ----------

def test_handler_500_does_not_leak_exception_detail(query_h, lambda_context, monkeypatch):
    def _boom(uid):
        raise RuntimeError("arn:aws:dynamodb:table/secret-internal-name")
    monkeypatch.setattr(query_h, "get_session_id", _boom)
    event = {"body": json.dumps({"question": "x"}),
             "requestContext": {"authorizer": {"claims": {"sub": "u1"}}}}
    resp = query_h.handler(event, lambda_context)
    assert resp["statusCode"] == 500
    assert "secret-internal-name" not in resp["body"]


# ---------- カスタムメトリクス：沈黙する失敗を数える（改善#03） ----------

def test_search_kb_failure_emits_retrieval_failure_metric(query_h, monkeypatch):
    class _Boom:
        def retrieve(self, **kw):
            raise RuntimeError("kb down")
    monkeypatch.setattr(query_h, "KNOWLEDGE_BASE_ID", "kb-test")
    monkeypatch.setattr(query_h, "bedrock_agent_runtime", _Boom())
    metric_mock = mock.MagicMock()
    monkeypatch.setattr(query_h.metrics, "add_metric", metric_mock)
    query_h._search_kb("q")
    metric_mock.assert_called_once_with(
        name="RetrievalFailure", unit=query_h.MetricUnit.Count, value=1)


def test_search_kb_unconfigured_emits_retrieval_failure_metric(query_h, monkeypatch):
    monkeypatch.setattr(query_h, "KNOWLEDGE_BASE_ID", "")
    metric_mock = mock.MagicMock()
    monkeypatch.setattr(query_h.metrics, "add_metric", metric_mock)
    query_h._search_kb("q")
    metric_mock.assert_called_once_with(
        name="RetrievalFailure", unit=query_h.MetricUnit.Count, value=1)


def test_search_kb_zero_hits_does_not_emit_metric(query_h, monkeypatch):
    # 正常に0件は障害ではない＝メトリクスも計上しない（ここが逆方向の退行を防ぐ）
    class _Empty:
        def retrieve(self, **kw):
            return {"retrievalResults": []}
    monkeypatch.setattr(query_h, "KNOWLEDGE_BASE_ID", "kb-test")
    monkeypatch.setattr(query_h, "bedrock_agent_runtime", _Empty())
    metric_mock = mock.MagicMock()
    monkeypatch.setattr(query_h.metrics, "add_metric", metric_mock)
    query_h._search_kb("q")
    metric_mock.assert_not_called()


def test_opensearch_hybrid_index_not_found_does_not_emit_metric(query_h, monkeypatch):
    # 文書ゼロの新環境（index_not_found）は正常状態。メトリクスは計上しない
    monkeypatch.setattr(query_h, "get_vector_store_endpoint", lambda: "https://example.com")
    monkeypatch.setattr(query_h, "get_embedding", lambda text: [0.1, 0.2])

    class _Client:
        def search(self, **kw):
            raise RuntimeError("index_not_found_exception: no such index")
    monkeypatch.setattr(query_h, "get_opensearch_client", lambda endpoint: _Client())
    metric_mock = mock.MagicMock()
    monkeypatch.setattr(query_h.metrics, "add_metric", metric_mock)
    result = query_h._search_opensearch_hybrid("q")
    assert result == []
    metric_mock.assert_not_called()


def test_opensearch_hybrid_real_failure_emits_metric(query_h, monkeypatch):
    monkeypatch.setattr(query_h, "get_vector_store_endpoint", lambda: "https://example.com")
    monkeypatch.setattr(query_h, "get_embedding", lambda text: [0.1, 0.2])

    class _Client:
        def search(self, **kw):
            raise RuntimeError("connection refused")
    monkeypatch.setattr(query_h, "get_opensearch_client", lambda endpoint: _Client())
    metric_mock = mock.MagicMock()
    monkeypatch.setattr(query_h.metrics, "add_metric", metric_mock)
    result = query_h._search_opensearch_hybrid("q")
    assert result is None
    metric_mock.assert_called_once_with(
        name="RetrievalFailure", unit=query_h.MetricUnit.Count, value=1)


def test_opensearch_hybrid_no_endpoint_emits_metric(query_h, monkeypatch):
    monkeypatch.setattr(query_h, "get_vector_store_endpoint", lambda: "")
    metric_mock = mock.MagicMock()
    monkeypatch.setattr(query_h.metrics, "add_metric", metric_mock)
    result = query_h._search_opensearch_hybrid("q")
    assert result is None
    metric_mock.assert_called_once_with(
        name="RetrievalFailure", unit=query_h.MetricUnit.Count, value=1)


def test_save_conversation_failure_emits_metric(query_h, monkeypatch):
    class _Table:
        def put_item(self, **kw):
            raise RuntimeError("throttled")
    monkeypatch.setattr(query_h.dynamodb, "Table", lambda name: _Table())
    metric_mock = mock.MagicMock()
    monkeypatch.setattr(query_h.metrics, "add_metric", metric_mock)
    query_h.save_conversation("u1", "s1", "q", "a")
    metric_mock.assert_called_once_with(
        name="ConversationSaveFailure", unit=query_h.MetricUnit.Count, value=1)


def test_save_session_failure_emits_metric(query_h, monkeypatch):
    class _Table:
        def put_item(self, **kw):
            raise RuntimeError("throttled")
    monkeypatch.setattr(query_h.dynamodb, "Table", lambda name: _Table())
    metric_mock = mock.MagicMock()
    monkeypatch.setattr(query_h.metrics, "add_metric", metric_mock)
    query_h.save_session("u1", "s1")
    metric_mock.assert_called_once_with(
        name="ConversationSaveFailure", unit=query_h.MetricUnit.Count, value=1)


def test_save_conversation_success_does_not_emit_metric(query_h, monkeypatch):
    monkeypatch.setattr(query_h.dynamodb, "Table", lambda name: mock.MagicMock())
    metric_mock = mock.MagicMock()
    monkeypatch.setattr(query_h.metrics, "add_metric", metric_mock)
    query_h.save_conversation("u1", "s1", "q", "a")
    metric_mock.assert_not_called()
