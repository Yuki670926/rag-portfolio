import json
import os
import time
import boto3
import urllib.parse
from pypdf import PdfReader
from io import BytesIO
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth
from aws_lambda_powertools import Logger, Tracer, Metrics
from aws_lambda_powertools.metrics import MetricUnit

logger = Logger()
tracer = Tracer()
metrics = Metrics()

s3_client = boto3.client("s3")
bedrock_client = boto3.client("bedrock-runtime", region_name="ap-northeast-1")
bedrock_agent_client = boto3.client("bedrock-agent", region_name="ap-northeast-1")
ssm_client = boto3.client("ssm", region_name="ap-northeast-1")
dynamodb = boto3.resource("dynamodb", region_name="ap-northeast-1")

# vector_store_type: s3_vectors（KB マネージド経路のみ）/ opensearch（自前経路のみ）/
# dual（両方＝fast/高精度の二段検索）。正本は常に S3 の PDF で、両ストアは
# 冪等に再生成できる派生インデックス（dual-write アンチパターンには該当しない）。
VECTOR_STORE_TYPE = os.environ.get("VECTOR_STORE_TYPE", "opensearch")
KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")
DATA_SOURCE_ID = os.environ.get("DATA_SOURCE_ID", "")
SSM_ENDPOINT_PARAM = os.environ.get("SSM_ENDPOINT_PARAM", "")
PDF_INDEXES_TABLE = os.environ.get("PDF_INDEXES_TABLE", "")
INDEX_NAME = "documents"
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50

def get_vector_store_endpoint():
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
        # OpenSearch Serverless NextGen は scale-to-zero。アイドル後の初回書込みは
        # コレクション暖機で既定 10 秒を超えタイムアウトする。timeout を延ばし、
        # タイムアウト時は再試行（暖機後は即応）。ingest Lambda は timeout=300s。
        timeout=30,
        max_retries=3,
        retry_on_timeout=True
    )

def ensure_index(client):
    if client.indices.exists(INDEX_NAME):
        return
    mappings = {
        "properties": {
            "embedding": {"type": "knn_vector", "dimension": 1024},
            "text": {"type": "text"},
            "source": {"type": "keyword"},   # term 検索で upsert/delete するため keyword
            "chunk_index": {"type": "integer"}
        }
    }
    # BM25（高精度モードのハイブリッド検索）の日本語品質のため kuromoji を試行。
    # Serverless で未サポートの場合は standard アナライザにフォールバック（英字・型番は拾える）。
    try:
        client.indices.create(index=INDEX_NAME, body={
            "settings": {
                "index.knn": True,
                "analysis": {"analyzer": {"ja": {
                    "type": "custom",
                    "tokenizer": "kuromoji_tokenizer",
                    "filter": ["kuromoji_baseform", "lowercase"]
                }}}
            },
            "mappings": {**mappings, "properties": {
                **mappings["properties"],
                "text": {"type": "text", "analyzer": "ja"}
            }}
        })
        logger.info(f"Index {INDEX_NAME} created (kuromoji)")
    except Exception as e:
        logger.warning(f"kuromoji index creation failed, falling back to standard: {e}")
        client.indices.create(index=INDEX_NAME, body={
            "settings": {"index.knn": True},
            "mappings": mappings
        })
        logger.info(f"Index {INDEX_NAME} created (standard)")

def chunk_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    words = text.split()
    chunks = []
    for i in range(0, len(words), chunk_size - overlap):
        chunk = " ".join(words[i:i + chunk_size])
        if chunk:
            chunks.append(chunk)
    return chunks

def get_embedding(text):
    response = bedrock_client.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText": text})
    )
    body = json.loads(response["body"].read())
    return body["embedding"]

# ---------- fast 経路（Bedrock KB / S3 Vectors） ----------

def start_kb_sync():
    """KB のデータ同期ジョブを開始（fire-and-forget）。KB が S3 を差分同期するため、
    作成・削除どちらのイベントでもこの1本で反映される。実行中なら次の同期に任せて成功扱い。"""
    if not KNOWLEDGE_BASE_ID or not DATA_SOURCE_ID:
        raise RuntimeError("KB IDs not configured")
    try:
        response = bedrock_agent_client.start_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
        )
        job_id = response["ingestionJob"]["ingestionJobId"]
        logger.info(f"Started KB ingestion job: {job_id}")
        metrics.add_metric(name="KBIngestionStarted", unit=MetricUnit.Count, value=1)
        return f"job:{job_id}"
    except bedrock_agent_client.exceptions.ConflictException:
        logger.warning("Ingestion job already in progress; will be picked up by next sync")
        return "job:in-progress"

# ---------- precise 経路（OpenSearch 自前パイプライン） ----------

def delete_document_chunks(client, key):
    """同一 source のチャンクを検索して個別削除（冪等）。
    （Serverless は delete_by_query 非対応の可能性があるため search+delete。
      また検索インデックスの反映には遅延があり、削除済み文書が stale に返ることが
      あるため、(a) 404 は無視 (b) 既に試行した id だけが返ったら終了、で収束させる。）"""
    deleted = 0
    seen = set()
    while True:
        resp = client.search(index=INDEX_NAME, body={
            "size": 200, "_source": False,
            "query": {"term": {"source": key}}
        })
        ids = [h["_id"] for h in resp["hits"]["hits"] if h["_id"] not in seen]
        if not ids:
            break
        for _id in ids:
            seen.add(_id)
            try:
                client.delete(index=INDEX_NAME, id=_id)
                deleted += 1
            except Exception:
                pass  # 404（反映遅延で stale に見えていた削除済み doc）等は冪等に無視
    if deleted:
        logger.info(f"Deleted {deleted} old chunks for {key}")
    return deleted

def upsert_document(client, bucket, key):
    """upsert：旧チャンク削除 → 抽出 → チャンク → 埋め込み → 索引。
    再アップロード（上書き）でもチャンクが重複しない。"""
    response = s3_client.get_object(Bucket=bucket, Key=key)
    reader = PdfReader(BytesIO(response["Body"].read()))
    full_text = ""
    for page in reader.pages:
        full_text += (page.extract_text() or "") + "\n"
    chunks = chunk_text(full_text)
    logger.info(f"{key}: {len(chunks)} chunks")

    delete_document_chunks(client, key)
    for i, chunk in enumerate(chunks):
        doc = {
            "text": chunk,
            "embedding": get_embedding(chunk),
            "source": key,
            "chunk_index": i
        }
        client.index(index=INDEX_NAME, body=doc)
    return len(chunks)

def put_ready_flag(key, chunks):
    # precise（OpenSearch）側の per-doc 準備完了フラグ。失敗しても本体は成功扱い（付帯情報）。
    if not PDF_INDEXES_TABLE:
        return
    try:
        dynamodb.Table(PDF_INDEXES_TABLE).put_item(Item={
            "user_id": "shared",
            "pdf_name": os.path.basename(key),
            "status": "ready",
            "chunks": chunks,
            "indexed_at": int(time.time()),
        })
    except Exception as e:
        logger.warning(f"Failed to write readiness flag for {key}: {e}")

def delete_ready_flag(key):
    if not PDF_INDEXES_TABLE:
        return
    try:
        dynamodb.Table(PDF_INDEXES_TABLE).delete_item(
            Key={"user_id": "shared", "pdf_name": os.path.basename(key)})
    except Exception as e:
        logger.warning(f"Failed to delete readiness flag for {key}: {e}")

# ---------- handler ----------

@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event, context):
    # S3 イベント（作成/削除）を仕分け
    created, removed = [], []
    for record in event.get("Records", []):
        name = record.get("eventName", "")
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        if name.startswith("ObjectCreated"):
            created.append((bucket, key))
        elif name.startswith("ObjectRemoved"):
            removed.append((bucket, key))
    logger.info(f"event: created={[k for _, k in created]} removed={[k for _, k in removed]}")

    # 両経路を分離した try/except で実行：片方の障害がもう片方を巻き込まない。
    # 片方でも失敗したら最後に例外を上げ、Lambda 非同期リトライ（→DLQ）に乗せる。
    # 全処理が冪等（KB=差分同期 / OpenSearch=upsert・delete）なので再実行は安全。
    results = {}

    if VECTOR_STORE_TYPE in ("s3_vectors", "dual"):
        try:
            results["fast"] = start_kb_sync()
        except Exception as e:
            logger.exception(f"fast(KB) path failed: {e}")
            results["fast"] = "error"

    if VECTOR_STORE_TYPE in ("opensearch", "dual"):
        try:
            endpoint = get_vector_store_endpoint()
            if not endpoint:
                raise RuntimeError("OpenSearch endpoint not configured")
            os_client = get_opensearch_client(endpoint)
            ensure_index(os_client)
            for bucket, key in created:
                n = upsert_document(os_client, bucket, key)
                put_ready_flag(key, n)
            for _, key in removed:
                delete_document_chunks(os_client, key)
                delete_ready_flag(key)
            results["precise"] = "ok"
        except Exception as e:
            logger.exception(f"precise(OpenSearch) path failed: {e}")
            results["precise"] = "error"

    logger.info(f"ingest results: {results}")
    if "error" in results.values():
        raise RuntimeError(f"partial failure: {results}")
    return {"statusCode": 200, "body": json.dumps(results)}
