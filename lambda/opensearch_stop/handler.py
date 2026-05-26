import os
import boto3
import json

opensearch_client = boto3.client("opensearchserverless", region_name="ap-northeast-1")
sns_client = boto3.client("sns", region_name="ap-northeast-1")

COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")

def get_collection_id():
    """コレクションIDを取得"""
    try:
        response = opensearch_client.list_collections(
            collectionFilters={"name": COLLECTION_NAME}
        )
        collections = response.get("collectionSummaries", [])
        if not collections:
            print(f"Collection not found: {COLLECTION_NAME}")
            return None
        return collections[0]["id"]
    except Exception as e:
        print(f"List collections error: {str(e)}")
        return None

def delete_collection(collection_id):
    """OpenSearch Collectionを削除"""
    try:
        opensearch_client.delete_collection(id=collection_id)
        print(f"Collection deleted: {collection_id}")
    except Exception as e:
        print(f"Collection deletion error: {str(e)}")
        raise

def send_notification(message):
    """SNSで通知"""
    if not SNS_TOPIC_ARN:
        return
    try:
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject="OpenSearch停止完了",
            Message=message
        )
    except Exception as e:
        print(f"SNS error: {str(e)}")

def handler(event, context):
    try:
        print("Starting OpenSearch collection deletion...")

        # ① コレクションIDを取得
        collection_id = get_collection_id()
        if not collection_id:
            message = f"OpenSearch停止スキップ（コレクションが存在しない）: {COLLECTION_NAME}"
            print(message)
            send_notification(message)
            return {
                "statusCode": 200,
                "body": json.dumps({"message": "Collection not found, skipping"})
            }

        # ② OpenSearch Collectionを削除
        delete_collection(collection_id)

        # ③ SNSで完了通知
        message = f"""OpenSearch停止完了

コレクション名: {COLLECTION_NAME}
コレクションID: {collection_id}
"""
        send_notification(message)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "OpenSearch stopped successfully",
                "collection_id": collection_id
            })
        }

    except Exception as e:
        error_message = f"OpenSearch停止失敗: {str(e)}"
        print(error_message)
        send_notification(error_message)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }