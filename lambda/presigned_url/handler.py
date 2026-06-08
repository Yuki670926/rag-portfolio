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

BUCKET_NAME = os.environ.get("DOCUMENTS_BUCKET", "")
EXPIRATION = 300

def handler(event, context):
    try:
        body = json.loads(event.get("body", "{}"))
        filename = body.get("filename", "")
        content_type = body.get("content_type", "application/pdf")

        if not filename:
            return {
                "statusCode": 400,
                "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": "ファイル名が空です"})
            }

        if not filename.endswith(".pdf"):
            return {
                "statusCode": 400,
                "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": "PDFファイルのみアップロード可能です"})
            }

        presigned_url = s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": BUCKET_NAME,
                "Key": f"documents/{filename}",
                "ContentType": content_type,
            },
            ExpiresIn=EXPIRATION,
            HttpMethod="PUT"
        )

        return {
            "statusCode": 200,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({
                "upload_url": presigned_url,
                "key": f"documents/{filename}",
                "expires_in": EXPIRATION
            })
        }

    except ClientError as e:
        print(f"S3 Error: {str(e)}")
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": str(e)})
        }
