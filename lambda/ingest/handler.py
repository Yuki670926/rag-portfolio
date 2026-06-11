import json
import os
import time
import boto3
import urllib.parse
from datetime import datetime, timezone, timedelta
from botocore.exceptions import ClientError
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
    # フォールバックの発火は「kuromoji/analyzer 未サポートを示すエラー」に限定する。
    # 一時的なタイムアウト等まで握ると standard で index が確定し、以後 exists() で
    # 素通り＝日本語 BM25 品質が恒久ダウングレードして環境間差異も不可視になるため、
    # それ以外のエラーは raise して非同期リトライに任せる。
    # already-exists は並行 ingest の勝者がいるだけなので成功扱い。
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
        # 実エラー文言の全文を記録（NextGen の拒否文言が下記語彙に合わない場合の調査用。
        # 合わないと毎回 raise→DLQ で「index 永久未作成」になるが、無音化はしない）
        logger.error(f"kuromoji index creation failed (raw): {e}")
        msg = str(e).lower()
        if "resource_already_exists" in msg:
            logger.info("Index already exists (parallel create); continuing")
            return
        if not any(w in msg for w in ("kuromoji", "analyzer", "tokenizer", "analysis")):
            raise  # 一時障害（timeout/スロットル等）はフォールバックせずリトライへ
        logger.warning(f"kuromoji unsupported, falling back to standard: {e}")
        try:
            client.indices.create(index=INDEX_NAME, body={
                "settings": {"index.knn": True},
                "mappings": mappings
            })
            logger.info(f"Index {INDEX_NAME} created (standard)")
        except Exception as e2:
            if "resource_already_exists" in str(e2).lower():
                return
            raise
    # 実効アナライザを記録：どちらのマッピングで作られたかを環境間で検知可能にする
    try:
        mapping = client.indices.get_mapping(index=INDEX_NAME)
        logger.info(f"effective index mapping: {json.dumps(mapping, default=str)[:500]}")
    except Exception as e:
        logger.warning(f"get_mapping failed (non-fatal): {e}")

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

def _find_covering_job(event_time):
    """イベント発生時刻より後に開始されたジョブの id を返す（無ければ None）。
    S3 は強整合のため「イベント時刻より後に開始された一括同期ジョブ」は当該
    オブジェクトを必ず走査対象に含む＝そのジョブが成果として使える。
    条件は2つ：
      (1) status が生きている（STARTING/IN_PROGRESS/COMPLETE）。FAILED/STOPPED を
          カバー扱いすると「取り込み漏れの無音化」が再発するため除外し raise 側に倒す
      (2) startedAt がイベント時刻＋2秒以降。S3 と Bedrock のクロックずれで
          「実はイベント前に開始したジョブ」を誤ってカバー判定しないための安全マージン"""
    if event_time is None:
        return None
    try:
        jobs = bedrock_agent_client.list_ingestion_jobs(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
            sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
            maxResults=5,
        ).get("ingestionJobSummaries", [])
        threshold = event_time + timedelta(seconds=2)
        for j in jobs:
            started = j.get("startedAt")
            if (started and started >= threshold
                    and j.get("status") in ("STARTING", "IN_PROGRESS", "COMPLETE")):
                return j.get("ingestionJobId")
    except Exception as e:
        logger.warning(f"covering-job check failed (treated as not covered): {e}")
    return None

def start_kb_sync(event_time=None):
    """KB のデータ同期ジョブを開始（fire-and-forget）。KB が S3 を差分同期するため、
    作成・削除どちらのイベントでもこの1本で反映される。"""
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
        # 実行中ジョブと衝突（KB は同時 1 ジョブ）。定期同期は存在しないため
        # 無条件に「次の同期に任せる」と取り込み漏れが恒久化し得る。一方で
        # 無条件 raise は連続アップロード時に DLQ 常態化＋precise 再実行を招く。
        # → 「イベント時刻より後に開始されたジョブ」があれば本オブジェクトは
        #   カバー済みなので、そのジョブを成果として成功扱いにする。
        #   カバー外のときだけ raise → 非同期リトライ（2回・約3分）→ DLQ で可視化。
        covering = _find_covering_job(event_time)
        if covering:
            logger.info(f"Conflict, but job {covering} (started after event) covers this object")
            return f"job:{covering}"
        logger.warning("Ingestion job in progress and does not cover this event; raising for async retry")
        raise

def put_fast_flag(key, job_ref):
    # fast(KB) 側の per-doc 記録：「この文書のイベントで起動した同期ジョブ id」。
    # KB はバケット一括の同期ジョブ方式で、ジョブのグローバル状態だけでは
    # 「この文書が索引済みか」を判定できない（アップロード直後の /status polling が
    # 前回ジョブの COMPLETE を拾う偽陽性レースがある）ため、文書→ジョブの対応を残す。
    # precise のフラグと同じテーブルに "<pdf>#fast" キーで同居。
    # 注意：この記録は /status の fast 判定の唯一の根拠（失敗すると当該文書の表示が
    # ready にならない）。ただし実害は表示のみ（索引自体は完了する）ため、
    # 書き込み失敗で invocation 全体は落とさない。
    if not PDF_INDEXES_TABLE:
        return
    try:
        dynamodb.Table(PDF_INDEXES_TABLE).put_item(Item={
            "user_id": "shared",
            "pdf_name": f"{os.path.basename(key)}#fast",
            "job_id": job_ref.replace("job:", ""),
            "started_at": int(time.time()),
        })
    except Exception as e:
        logger.warning(f"Failed to write fast flag for {key}: {e}")

def delete_fast_flag(key):
    if not PDF_INDEXES_TABLE:
        return
    try:
        dynamodb.Table(PDF_INDEXES_TABLE).delete_item(
            Key={"user_id": "shared", "pdf_name": f"{os.path.basename(key)}#fast"})
    except Exception as e:
        logger.warning(f"Failed to delete fast flag for {key}: {e}")

# ---------- precise 経路（OpenSearch 自前パイプライン） ----------

def delete_document_chunks(client, key, keep_ids=frozenset()):
    """同一 source のチャンクを検索して個別削除（冪等）。keep_ids は残す id
    （upsert 直後の新チャンク）。
    （Serverless は delete_by_query 非対応の可能性があるため search+delete。
      検索の反映遅延で削除済み doc が stale に返り得るため (a) 404 は無視
      (b) 照会済み id は must_not で検索段階から除外＝stale が上位 200 件を
      占有して 201 件目以降が隠れる取りこぼしも防ぐ、で収束させる。）"""
    deleted = 0
    seen = set()
    while True:
        must_not = [{"ids": {"values": sorted(seen)}}] if seen else []
        resp = client.search(index=INDEX_NAME, body={
            "size": 200, "_source": False,
            "query": {"bool": {
                "filter": [{"term": {"source": key}}],
                "must_not": must_not,
            }}
        })
        ids = [h["_id"] for h in resp["hits"]["hits"]]
        if not ids:
            break
        for _id in ids:
            seen.add(_id)
            if _id in keep_ids:
                continue
            try:
                client.delete(index=INDEX_NAME, id=_id)
                deleted += 1
            except Exception:
                pass  # 404（反映遅延で stale に見えていた削除済み doc）等は冪等に無視
    if deleted:
        logger.info(f"Deleted {deleted} old chunks for {key}")
    return deleted

def upsert_document(client, bucket, key):
    """upsert：決定的 _id（"<key>#<i>"）で在位上書き → 残骸を掃除。
    決定的 _id により再実行・重複配信は同じ doc への上書きになり、冪等性が
    検索の反映遅延に依存しない（重複チャンク窓の構造的解消）。先に索引して
    から旧分（旧自動 _id の残骸・チャンク数縮小分）を消すため、従来の
    「削除→再投入」で生じていた検索空白窓も無い。"""
    response = s3_client.get_object(Bucket=bucket, Key=key)
    reader = PdfReader(BytesIO(response["Body"].read()))
    full_text = ""
    for page in reader.pages:
        full_text += (page.extract_text() or "") + "\n"
    chunks = chunk_text(full_text)
    logger.info(f"{key}: {len(chunks)} chunks")

    expected = set()
    for i, chunk in enumerate(chunks):
        doc = {
            "text": chunk,
            "embedding": get_embedding(chunk),
            "source": key,
            "chunk_index": i
        }
        client.index(index=INDEX_NAME, id=f"{key}#{i}", body=doc)
        expected.add(f"{key}#{i}")
    delete_document_chunks(client, key, keep_ids=expected)
    return len(chunks)

def object_exists(bucket, key):
    """削除イベントの実行時点でオブジェクトが再作成されていないかの確認用。
    「不存在」と断定するのは 404 のみ。スロットル等の一時障害まで False に丸めると、
    生きている文書のチャンクを削除する方向（データ消失側）に倒れるため raise して
    非同期リトライに任せる（s3:ListBucket 付与済みのため不存在は 404 で返る）。"""
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        code = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code == 404:
            return False
        raise

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
    # S3 イベント（作成/削除）を仕分け。eventTime は KB 同期ジョブの
    # カバレッジ判定（Conflict 時）に使うため最大値を控える。
    created, removed = [], []
    latest_event_time = None
    for record in event.get("Records", []):
        name = record.get("eventName", "")
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        try:
            et = datetime.fromisoformat(record["eventTime"].replace("Z", "+00:00"))
            if latest_event_time is None or et > latest_event_time:
                latest_event_time = et
        except Exception:
            pass  # eventTime 欠落時はカバレッジ判定をスキップ（無条件 raise 側に倒れる）
        if name.startswith("ObjectCreated"):
            created.append((bucket, key))
        elif name.startswith("ObjectRemoved"):
            removed.append((bucket, key))
    logger.info(f"event: created={[k for _, k in created]} removed={[k for _, k in removed]}")

    # 削除イベントのうち、実行時点でオブジェクトが再作成されているものは除外する。
    # 非同期リトライでイベントの実行順序が実質入れ替わったとき、後から走る削除が
    # 同名再アップロードの結果（チャンク・フラグ）を壊さないため（last-writer-wins）。
    still_removed = []
    for bucket, key in removed:
        if object_exists(bucket, key):
            logger.info(f"skip delete for {key}: object re-created (newer upload wins)")
        else:
            still_removed.append((bucket, key))

    # 両経路を分離した try/except で実行：片方の障害がもう片方を巻き込まない。
    # 片方でも失敗したら最後に例外を上げ、Lambda 非同期リトライ（→DLQ）に乗せる。
    # 全処理が冪等（KB=差分同期 / OpenSearch=upsert・delete）なので再実行は安全。
    results = {}

    if VECTOR_STORE_TYPE in ("s3_vectors", "dual"):
        try:
            job_ref = start_kb_sync(latest_event_time)
            # 文書→起動ジョブの対応を記録（/status の per-doc 判定用）。
            # 削除イベントの文書は記録ごと消す（polling 対象から外れる）。
            for _, key in created:
                put_fast_flag(key, job_ref)
            for _, key in still_removed:
                delete_fast_flag(key)
            results["fast"] = job_ref
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
            for _, key in still_removed:
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
