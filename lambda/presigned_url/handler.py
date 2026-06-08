import json
import os
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# SSE-KMS バケットへの presigned PUT は AWS Signature Version 4 が必須。
# 既定の SigV2 だと S3 が 400 InvalidArgument
# ("Requests specifying SSE with AWS KMS managed keys require AWS Signature Version 4.") を返す。
s3_client = boto3.client(
    "s3",
    region_name="ap-northeast-1",
    config=Config(signature_version="s3v4"),
)
dynamodb = boto3.resource("dynamodb", region_name="ap-northeast-1")

BUCKET_NAME = os.environ.get("DOCUMENTS_BUCKET", "")
PDF_INDEXES_TABLE = os.environ.get("PDF_INDEXES_TABLE", "")
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
