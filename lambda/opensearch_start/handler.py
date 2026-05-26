import os
import boto3
import json
import time

opensearch_client = boto3.client("opensearchserverless", region_name="ap-northeast-1")
ssm_client = boto3.client("ssm", region_name="ap-northeast-1")
dynamodb = boto3.resource("dynamodb", region_name="ap-northeast-1")
lambda_client = boto3.client("lambda", region_name="ap-northeast-1")
sns_client = boto3.client("sns", region_name="ap-northeast-1")

COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "")
SSM_ENDPOINT_PARAM = os.environ.get("SSM_ENDPOINT_PARAM", "")
PDF_INDEXES_TABLE = os.environ.get("PDF_INDEXES_TABLE", "")
INGEST_LAMBDA_NAME = os.environ.get("INGEST_LAMBDA_NAME", "")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")

def create_collection():
    """OpenSearch Collectionを作成"""
    try:
        response = opensearch_client.create_collection(
            name=COLLECTION_NAME,
            type="VECTORSEARCH"
        )
        return response["createCollectionDetail"]["id"]
    except Exception as e:
        print(f"Collection creation error: {str(e)}")
        raise

def wait_for_collection_active(collection_id):
    """Collectionがアクティブになるまで待機"""
    print("Waiting for collection to become active...")
    while True:
        response = opensearch_client.batch_get_collection(
            ids=[collection_id]
        )
        status = response["collectionDetails"][0]["status"]
        print(f"Collection status: {status}")
        if status == "ACTIVE":
            endpoint = response["collectionDetails"][0]["collectionEndpoint"]
            return endpoint
        elif status == "FAILED":
            raise Exception("Collection creation failed")
        time.sleep(30)

def save_endpoint_to_ssm(endpoint):
    """SSMにエンドポイントを保存"""
    ssm_client.put_parameter(
        Name=SSM_ENDPOINT_PARAM,
        Value=endpoint,
        Type="SecureString",
        Overwrite=True
    )
    print(f"Endpoint saved to SSM: {endpoint}")

def get_pdf_indexes():
    """DynamoDBからインデックス済みPDF一覧を取得"""
    try:
        table = dynamodb.Table(PDF_INDEXES_TABLE)
        response = table.scan()
        return response.get("Items", [])
    except Exception as e:
        print(f"DynamoDB scan error: {str(e)}")
        return []

def trigger_ingest(s3_key, bucket_name):
    """ingest Lambdaを呼び出してPDFを再インデックス"""
    try:
        payload = {
            "Records": [{
                "s3": {
                    "bucket": {"name": bucket_name},
                    "object": {"key": s3_key}
                }
            }]
        }
        lambda_client.invoke(
            FunctionName=INGEST_LAMBDA_NAME,
            InvocationType="Event",
            Payload=json.dumps(payload)
        )
        print(f"Triggered ingest for: {s3_key}")
    except Exception as e:
        print(f"Ingest trigger error: {str(e)}")

def send_notification(message):
    """SNSで通知"""
    if not SNS_TOPIC_ARN:
        return
    try:
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject="OpenSearch起動完了",
            Message=message
        )
    except Exception as e:
        print(f"SNS error: {str(e)}")

def handler(event, context):
    try:
        print("Starting OpenSearch collection creation...")

        # ① OpenSearch Collectionを作成
        collection_id = create_collection()
        print(f"Collection created: {collection_id}")

        # ② Collectionがアクティブになるまで待機
        endpoint = wait_for_collection_active(collection_id)
        print(f"Collection active: {endpoint}")

        # ③ SSMにエンドポイントを保存
        save_endpoint_to_ssm(endpoint)

        # ④ DynamoDBからインデックス済みPDF一覧を取得
        pdf_indexes = get_pdf_indexes()
        print(f"Found {len(pdf_indexes)} PDFs to re-index")

        # ⑤ 各PDFを再インデックス
        for pdf in pdf_indexes:
            trigger_ingest(pdf["pdf_name"], pdf.get("bucket_name", ""))

        # ⑥ SNSで完了通知
        message = f"""OpenSearch起動完了

コレクション名: {COLLECTION_NAME}
エンドポイント: {endpoint}
再インデックス対象PDF数: {len(pdf_indexes)}件
"""
        send_notification(message)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "OpenSearch started successfully",
                "endpoint": endpoint,
                "reindexed_count": len(pdf_indexes)
            })
        }

    except Exception as e:
        error_message = f"OpenSearch起動失敗: {str(e)}"
        print(error_message)
        send_notification(error_message)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }