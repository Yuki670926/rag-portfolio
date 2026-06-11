import json
import os
import time
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# SSE-KMS バケットへの presigned PUT は AWS Signature Version 4 が必須。
# 既定の SigV2 だと S3 が 400 InvalidArgument
# ("Requests specifying SSE with AWS KMS managed keys require AWS Signature Version 4.") を返す。
# endpoint_url でリージョナルエンドポイントを強制：既定だとグローバル(s3.amazonaws.com)の
# URL が生成されることがあり、作りたてのバケットは DNS 伝播まで us-east-1 から 307 が返る。
# リダイレクト応答には CORS ヘッダが無くブラウザがブロックする（新環境構築直後に顕在化）。
s3_client = boto3.client(
    "s3",
    region_name="ap-northeast-1",
    endpoint_url="https://s3.ap-northeast-1.amazonaws.com",
    config=Config(signature_version="s3v4"),
)
dynamodb = boto3.resource("dynamodb", region_name="ap-northeast-1")
bedrock_agent = boto3.client("bedrock-agent", region_name="ap-northeast-1")

BUCKET_NAME = os.environ.get("DOCUMENTS_BUCKET", "")
PDF_INDEXES_TABLE = os.environ.get("PDF_INDEXES_TABLE", "")
# 既定は s3_vectors（IaC の fail-safe 既定と統一）
VECTOR_STORE_TYPE = os.environ.get("VECTOR_STORE_TYPE", "s3_vectors")
KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")
DATA_SOURCE_ID = os.environ.get("DATA_SOURCE_ID", "")
EXPIRATION = 300

CORS = {"Access-Control-Allow-Origin": "*"}


def _resp(status, body):
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body)}


def get_status(event):
    # GET /status?pdf=<filename>：索引化の「準備完了」を返す（フロントの polling 用）。
    # ドキュメントは現状グローバル共有のため user_id は定数 "shared"（マルチテナント化は別案件）。
    params = event.get("queryStringParameters") or {}
    pdf_name = params.get("pdf", "")
    if not pdf_name:
        return _resp(400, {"error": "pdf パラメータが必要です"})

    # ストア別の readiness：
    #   fast    = KB（S3 Vectors）。文書→起動ジョブの対応（ingest が記録）で per-doc 判定
    #   precise = OpenSearch。ingest が pdf_indexes に書く per-doc フラグで判定
    # 後方互換：トップレベル ready は「いずれかのストアで質問可能になったか」。
    # （dict は truthy のため `or` で繋ぐと常に fast 側が選ばれ、precise が先に
    #   ready でも反映されないバグがあった→ any() で判定する。）
    stores = {}
    if VECTOR_STORE_TYPE in ("s3_vectors", "dual"):
        stores["fast"] = _fast_ready(pdf_name)
    if VECTOR_STORE_TYPE in ("opensearch", "dual"):
        stores["precise"] = _precise_ready(pdf_name)

    body = {
        "ready": any(s.get("ready", False) for s in stores.values()) if stores else True,
        "stores": stores,
    }
    chunks = next((s["chunks"] for s in stores.values() if "chunks" in s), None)
    if chunks is not None:
        body["chunks"] = chunks  # 旧フロント互換（opensearch 単独時）
    return _resp(200, body)


def _fast_ready(pdf_name):
    # 「この文書のイベントで起動したジョブ」（ingest が pdf_indexes に "<pdf>#fast" で記録）
    # が COMPLETE なら ready。ジョブ単位のグローバル判定（最新ジョブ=COMPLETE）だと、
    # アップロード直後の初回 polling が前回ジョブの COMPLETE を拾って未索引なのに
    # 「✅ 完了」を出す偽陽性レースがあるため、per-doc 判定に揃える。
    # 記録が無い＝ジョブ未起動（S3 イベント処理前 or 起動失敗のリトライ待ち）= not ready。
    if not (KNOWLEDGE_BASE_ID and DATA_SOURCE_ID):
        return {"ready": True}  # KB 未設定時はブロックしない
    if not PDF_INDEXES_TABLE:
        return {"ready": False}
    try:
        item = dynamodb.Table(PDF_INDEXES_TABLE).get_item(
            Key={"user_id": "shared", "pdf_name": f"{pdf_name}#fast"}
        ).get("Item")
        if not item:
            return {"ready": False}
        job_id = item.get("job_id", "")
        jobs = bedrock_agent.list_ingestion_jobs(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
            sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
            maxResults=20,
        ).get("ingestionJobSummaries", [])
        status = next((j.get("status") for j in jobs
                       if j.get("ingestionJobId") == job_id), None)
        if status is None:
            # list の反映遅延でジョブ起動直後は一覧に出ないことがある。
            # フラグが新しいうちは「結果整合性待ち」として not ready に倒し、
            # 十分古ければ「直近 20 件より古い＝とうに完了したジョブ」とみなす。
            started_at = int(item.get("started_at", 0))
            if time.time() - started_at < 60:
                return {"ready": False}
            return {"ready": True}
        return {"ready": status == "COMPLETE"}
    except Exception as e:
        print(f"fast status error: {str(e)}")
        return {"ready": False}


def _precise_ready(pdf_name):
    if not PDF_INDEXES_TABLE:
        return {"ready": False}
    try:
        item = dynamodb.Table(PDF_INDEXES_TABLE).get_item(
            Key={"user_id": "shared", "pdf_name": pdf_name}
        ).get("Item")
        if item and item.get("status") == "ready":
            return {"ready": True, "chunks": int(item.get("chunks", 0))}
        return {"ready": False}
    except ClientError as e:
        print(f"DynamoDB Error: {str(e)}")
        return {"ready": False}


def create_presigned(event):
    # API GW プロキシ統合は空ボディ POST で body=None を渡す（.get の既定値は効かない）。
    # 未捕捉だと CORS ヘッダ無しの 502 になりブラウザで原因不明化するため 400 で返す。
    try:
        body = json.loads(event.get("body") or "{}")
    except (TypeError, ValueError):
        return _resp(400, {"error": "リクエストボディが不正です"})
    # basename 化：filename はユーザー入力。"../" 等のパス要素を落とし、
    # documents/ プレフィックス外への書き込み（パストラバーサル）を防ぐ。
    filename = os.path.basename(body.get("filename", ""))
    content_type = body.get("content_type", "application/pdf")

    if not filename:
        return _resp(400, {"error": "ファイル名が空です"})
    if not filename.endswith(".pdf"):
        return _resp(400, {"error": "PDFファイルのみアップロード可能です"})
    # OpenSearch の _id（"documents/<filename>#<i>"）は 512 バイト上限。
    # 超えると当該文書の索引が毎回失敗して DLQ 行きになるため入口で弾く。
    if len(filename.encode("utf-8")) > 400:
        return _resp(400, {"error": "ファイル名が長すぎます（400バイト以内にしてください）"})

    presigned_url = s3_client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": BUCKET_NAME,
            "Key": f"documents/{filename}",
            "ContentType": content_type,
        },
        ExpiresIn=EXPIRATION,
        HttpMethod="PUT",
    )

    # 同名 PDF の旧 readiness フラグを掃除（冪等・失敗しても発行は成功扱い）。
    # 再アップロード時、ingest がイベントを処理するまで前回の ready フラグが残り、
    # PUT 直後の初回 polling が「✅ 完了」を拾う偽陽性になるのを防ぐ。
    # 発行だけして PUT しなかった場合もフラグが消えるが、影響は /status の表示のみ
    # （索引と検索は無傷・次のアップロードで再生成される）。
    # 既知の残存レース（許容）：同名文書の「先行 ingest」が実行中だと、その完了時に
    # 旧版の ready が書き戻され偽陽性が狭い窓で再発し得る。表示のみの影響で、
    # 新しい ingest の完了で自己解消するため、世代管理（uploaded_at 条件付き書き込み）は
    # 必要になったら導入する。
    _clear_ready_flags(filename)

    return _resp(200, {
        "upload_url": presigned_url,
        "key": f"documents/{filename}",
        "expires_in": EXPIRATION,
    })


def _clear_ready_flags(pdf_name):
    if not PDF_INDEXES_TABLE:
        return
    table = dynamodb.Table(PDF_INDEXES_TABLE)
    for key_name in (pdf_name, f"{pdf_name}#fast"):
        try:
            table.delete_item(Key={"user_id": "shared", "pdf_name": key_name})
        except Exception as e:
            print(f"clear ready flag failed for {key_name}: {e}")


def handler(event, context):
    # 同一 Lambda で POST /upload（presigned 発行）と GET /status（準備完了照会）を処理。
    try:
        method = event.get("httpMethod", "POST")
        resource = event.get("resource", "") or event.get("path", "")
        if method == "GET" or resource.endswith("/status"):
            return get_status(event)
        return create_presigned(event)
    except ClientError as e:
        print(f"Error: {str(e)}")
        return _resp(500, {"error": str(e)})
    except Exception as e:
        # 未捕捉例外は API GW の 502（CORS ヘッダ無し）になり、ブラウザでは
        # 原因不明のネットワークエラーに見えるため、必ず CORS 付き 500 で返す
        # （query handler と同じ方針）。
        print(f"Unhandled error: {str(e)}")
        return _resp(500, {"error": "内部エラーが発生しました"})
