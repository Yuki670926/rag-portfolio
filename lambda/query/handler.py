import json
import os
import boto3
from datetime import datetime, timezone, timedelta
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth
from aws_lambda_powertools import Logger, Tracer, Metrics
from aws_lambda_powertools.metrics import MetricUnit

logger = Logger()
tracer = Tracer()
metrics = Metrics()

bedrock_client = boto3.client("bedrock-runtime", region_name="ap-northeast-1")
bedrock_agent_runtime = boto3.client("bedrock-agent-runtime", region_name="ap-northeast-1")
dynamodb = boto3.resource("dynamodb", region_name="ap-northeast-1")
ssm_client = boto3.client("ssm", region_name="ap-northeast-1")

VECTOR_STORE_TYPE = os.environ.get("VECTOR_STORE_TYPE", "opensearch")
KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")
SSM_ENDPOINT_PARAM = os.environ.get("SSM_ENDPOINT_PARAM", "")
CONVERSATIONS_TABLE = os.environ.get("CONVERSATIONS_TABLE", "")
SESSIONS_TABLE = os.environ.get("SESSIONS_TABLE", "")
INDEX_NAME = "documents"
TOP_K = 3
MAX_HISTORY = 5
TTL_DAYS = 90

def get_vector_store_endpoint():
    """SSM Parameter StoreからエンドポイントURLを取得"""
    if not SSM_ENDPOINT_PARAM:
        return ""
    try:
        response = ssm_client.get_parameter(
            Name=SSM_ENDPOINT_PARAM,
            WithDecryption=True
        )
        return response["Parameter"]["Value"]
    except Exception as e:
        logger.error(f"SSM error: {str(e)}")
        return ""

def get_aws_auth():
    credentials = boto3.Session().get_credentials()
    return AWS4Auth(
        credentials.access_key,
        credentials.secret_key,
        "ap-northeast-1",
        "aoss",
        session_token=credentials.token
    )

def get_opensearch_client(endpoint):
    host = endpoint.replace("https://", "")
    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=get_aws_auth(),
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        # query は同期 API（REST API Gateway の統合タイムアウトは上限 29 秒）。
        # NextGen は scale-to-zero でコールド時に暖機遅延があるため、29 秒を超えて
        # 504 を返さないよう「短め timeout × 1 リトライ」で最悪 16 秒に収める
        # （暖機が間に合えば成功、間に合わなければ graceful にエラー応答→再質問で復帰）。
        # コールドを“成功”させたい場合は collection を warm に保つ運用が必要（設計判断）。
        timeout=8,
        max_retries=1,
        retry_on_timeout=True
    )

def get_embedding(text):
    response = bedrock_client.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText": text})
    )
    body = json.loads(response["body"].read())
    return body["embedding"]

def _search_kb(question):
    # fast 経路：Bedrock KB Retrieve（埋め込み生成・検索は KB 側で実行）
    if not KNOWLEDGE_BASE_ID:
        logger.error("KNOWLEDGE_BASE_ID not configured")
        return []
    try:
        response = bedrock_agent_runtime.retrieve(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            retrievalQuery={"text": question},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": TOP_K}
            },
        )
        results = []
        for item in response.get("retrievalResults", []):
            results.append({
                "text": item.get("content", {}).get("text", ""),
                "source": item.get("location", {})
                              .get("s3Location", {})
                              .get("uri", "unknown"),
            })
        return results
    except Exception as e:
        logger.error(f"KB Retrieve error: {str(e)}")
        return []


def _rrf_merge(rank_lists, k=60, top=TOP_K):
    # Reciprocal Rank Fusion：スコア体系の異なるランキング（BM25 と kNN）を
    # 1/(k+rank) の和で融合する定番手法。スコアの正規化が不要で頑健。
    scores, docs = {}, {}
    for hits in rank_lists:
        for rank, h in enumerate(hits):
            _id = h["_id"]
            scores[_id] = scores.get(_id, 0.0) + 1.0 / (k + rank + 1)
            docs[_id] = h["_source"]
    return [docs[i] for i in sorted(scores, key=scores.get, reverse=True)[:top]]


def _search_opensearch_hybrid(question):
    """precise 経路：BM25（キーワード・略語/型番に強い）と kNN（意味検索）を
    別々に実行し RRF で融合。接続失敗（コールド等）は None を返し、呼び出し側で
    fast へフォールバックさせる。"""
    endpoint = get_vector_store_endpoint()
    if not endpoint:
        return None
    try:
        client = get_opensearch_client(endpoint)
        knn = client.search(index=INDEX_NAME, body={
            "size": TOP_K * 2,
            "query": {"knn": {"embedding": {
                "vector": get_embedding(question), "k": TOP_K * 2}}}
        })["hits"]["hits"]
        bm25 = client.search(index=INDEX_NAME, body={
            "size": TOP_K * 2,
            "query": {"match": {"text": question}}
        })["hits"]["hits"]
        return _rrf_merge([knn, bm25])
    except Exception as e:
        logger.error(f"OpenSearch hybrid error: {str(e)}")
        return None


def search_documents(question, mode="fast"):
    """構成とモードから実効バックエンドを決めて検索する。
    返り値: (contexts, used_mode, fallback)
      - 単独構成では構成側を優先（mode 指定は無視）
      - dual の precise がコールド/障害のときは fast へ自動フォールバック"""
    if VECTOR_STORE_TYPE == "s3_vectors":
        return _search_kb(question), "fast", False
    if VECTOR_STORE_TYPE == "opensearch":
        ctx = _search_opensearch_hybrid(question)
        return (ctx if ctx is not None else []), "precise", False
    # dual
    if mode == "precise":
        ctx = _search_opensearch_hybrid(question)
        if ctx is not None:
            return ctx, "precise", False
        logger.warning("precise unavailable (likely cold), falling back to fast")
        return _search_kb(question), "fast", True
    return _search_kb(question), "fast", False
    

def get_session_id(user_id):
    try:
        table = dynamodb.Table(SESSIONS_TABLE)
        response = table.query(
            KeyConditionExpression="user_id = :uid",
            ExpressionAttributeValues={":uid": user_id},
            ScanIndexForward=False,
            Limit=1
        )
        items = response.get("Items", [])
        if items:
            return items[0]["session_id"]
        return None
    except Exception as e:
        logger.error(f"DynamoDB session error: {str(e)}")
        return None

def get_conversation_history(user_id, session_id):
    if not session_id:
        return []
    try:
        table = dynamodb.Table(CONVERSATIONS_TABLE)
        response = table.query(
            KeyConditionExpression="user_id = :uid",
            ExpressionAttributeValues={":uid": user_id},
            ScanIndexForward=False,
            Limit=MAX_HISTORY
        )
        items = response.get("Items", [])
        # 古い順に並べ直す
        items.reverse()
        return items
    except Exception as e:
        logger.error(f"DynamoDB history error: {str(e)}")
        return []

def save_conversation(user_id, session_id, question, answer):
    try:
        table = dynamodb.Table(CONVERSATIONS_TABLE)
        now = datetime.now(timezone.utc)
        ttl = int((now + timedelta(days=TTL_DAYS)).timestamp())
        table.put_item(Item={
            "user_id": user_id,
            "timestamp": now.isoformat(),
            "session_id": session_id,
            "question": question,
            "answer": answer,
            "ttl": ttl
        })
    except Exception as e:
        logger.error(f"DynamoDB save error: {str(e)}")

def save_session(user_id, session_id):
    try:
        table = dynamodb.Table(SESSIONS_TABLE)
        now = datetime.now(timezone.utc)
        ttl = int((now + timedelta(days=TTL_DAYS)).timestamp())
        table.put_item(Item={
            "user_id": user_id,
            "last_accessed_at": now.isoformat(),
            "session_id": session_id,
            "ttl": ttl
        })
    except Exception as e:
        logger.error(f"DynamoDB session save error: {str(e)}")

def generate_answer(question, contexts, history):
    context_text = "\n\n".join([
        f"[出典: {c.get('source', 'unknown')}]\n{c.get('text', '')}"
        for c in contexts
    ])

    history_text = "\n".join([
        f"ユーザー: {h['question']}\nアシスタント: {h['answer']}"
        for h in history
    ])

    if contexts:
        prompt = f"""以下のドキュメントを元に、質問に答えてください。
ドキュメントに情報がない場合は、「ドキュメントに該当する情報がありません」と答えてください。
必ず出典を明示してください。

ドキュメント:
{context_text}

過去の会話:
{history_text}

質問: {question}

回答:"""
    else:
        prompt = f"""過去の会話:
{history_text}

質問: {question}

回答:"""

    response = bedrock_client.invoke_model(
        modelId="jp.anthropic.claude-haiku-4-5-20251001-v1:0",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}]
        })
    )
    body = json.loads(response["body"].read())
    return body["content"][0]["text"]

def warmup_opensearch():
    # 非同期起動の暖機専用（同期パスより長い timeout＝REST API GW の 29s 制約と無関係）。
    # コールド(scale-to-zero)からの scale-up を待つ。best-effort で、失敗しても無害。
    if VECTOR_STORE_TYPE not in ("opensearch", "dual"):
        return {"warmup": "skipped"}
    endpoint = get_vector_store_endpoint()
    if not endpoint:
        return {"warmup": "skipped", "reason": "no endpoint"}
    host = endpoint.replace("https://", "")
    warm_client = OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=get_aws_auth(),
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=20,
        max_retries=1,
        retry_on_timeout=True,
    )
    try:
        warm_client.search(index=INDEX_NAME, body={"size": 0})
        logger.info("warmup: collection ready")
        return {"warmup": "ready"}
    except Exception as e:
        # コールドの timeout でもリクエスト到達で scale-up は始まる。404(index無)等の応答も warm 扱い。
        logger.info(f"warmup: ping done (ignored): {e}")
        return {"warmup": "pinged"}


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event, context):
    # ログイン起点ウォーマー（Post-Auth トリガからの非同期起動 {"warmup": true}）。
    # OpenSearch collection を暖機して初回検索のコールド timeout を防ぐ（cold-start 対策 D）。
    if event.get("warmup"):
        return warmup_opensearch()

    try:
        body = json.loads(event.get("body", "{}"))
        question = body.get("question", "")
        mode = body.get("mode", "fast")  # fast | precise（dual のときのみ有効）
        if not question:
            return {
                "statusCode": 400,
                "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": "質問が空です"})
            }

        # user_idをLambda Authorizerのcontextから取得
        user_id = event.get("requestContext", {}).get("authorizer", {}).get("user_id", "anonymous")

        # セッション管理
        session_id = get_session_id(user_id)
        if not session_id:
            session_id = f"{user_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

        # 会話履歴取得
        history = get_conversation_history(user_id, session_id)

        # RAG検索（used_mode=実際に使った経路、fallback=precise 不可で fast に落ちたか）
        contexts, used_mode, fallback = search_documents(question, mode)

        # 回答生成
        answer = generate_answer(question, contexts, history)

        # 会話履歴・セッション保存
        save_conversation(user_id, session_id, question, answer)
        save_session(user_id, session_id)

        return {
            "statusCode": 200,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({
                "answer": answer,
                "sources": list(set([c.get("source") for c in contexts if c.get("source")])),
                "context_count": len(contexts),
                "mode": used_mode,
                "fallback": fallback
            }, ensure_ascii=False)
        }
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": str(e)})
        }