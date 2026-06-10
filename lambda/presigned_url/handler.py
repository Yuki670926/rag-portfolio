import json
import os
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
VECTOR_STORE_TYPE = os.environ.get("VECTOR_STORE_TYPE", "opensearch")
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

    # s3_vectors はベクトルストアが Bedrock KB の「一括取り込みジョブ」方式で、per-doc の
    # ready フラグが無い。アップロードで起動した最新の取り込みジョブが COMPLETE なら、その
    # 文書も検索可能とみなす（opensearch の per-doc ready と UX 上のパリティを取る）。
    if VECTOR_STORE_TYPE == "s3_vectors":
        return _status_s3_vectors()

    # opensearch：ingest が pdf_indexes に書く per-doc の ready フラグを見る。
    if not PDF_INDEXES_TABLE:
        return _resp(500, {"error": "status backend not configured"})
    try:
        item = dynamodb.Table(PDF_INDEXES_TABLE).get_item(
            Key={"user_id": "shared", "pdf_name": pdf_name}
        ).get("Item")
        if item and item.get("status") == "ready":
            return _resp(200, {"ready": True, "chunks": int(item.get("chunks", 0))})
        return _resp(200, {"ready": False})
    except ClientError as e:
        print(f"DynamoDB Error: {str(e)}")
        return _resp(500, {"error": str(e)})


def _status_s3_vectors():
    # 最新の取り込みジョブが COMPLETE なら ready。IN_PROGRESS/STARTING の間は not ready。
    if not (KNOWLEDGE_BASE_ID and DATA_SOURCE_ID):
        return _resp(200, {"ready": True})  # KB 未設定時はブロックしない
    try:
        jobs = bedrock_agent.list_ingestion_jobs(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
            sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
            maxResults=1,
        ).get("ingestionJobSummaries", [])
        if jobs and jobs[0].get("status") == "COMPLETE":
            return _resp(200, {"ready": True})
        return _resp(200, {"ready": False})
    except Exception as e:
        print(f"s3_vectors status error: {str(e)}")
        return _resp(200, {"ready": False})


def create_presigned(event):
    body = json.loads(event.get("body", "{}"))
    filename = body.get("filename", "")
    content_type = body.get("content_type", "application/pdf")

    if not filename:
        return _resp(400, {"error": "ファイル名が空です"})
    if not filename.endswith(".pdf"):
        return _resp(400, {"error": "PDFファイルのみアップロード可能です"})

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
    return _resp(200, {
        "upload_url": presigned_url,
        "key": f"documents/{filename}",
        "expires_in": EXPIRATION,
    })


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
