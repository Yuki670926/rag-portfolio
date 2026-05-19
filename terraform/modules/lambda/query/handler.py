import json
import os
import boto3

bedrock_client = boto3.client("bedrock-runtime", region_name="ap-northeast-1")

OPENSEARCH_ENDPOINT = os.environ.get("OPENSEARCH_ENDPOINT", "")
INDEX_NAME = "documents"
TOP_K = 3


def get_embedding(text):
    """Bedrock Titan Embeddingsでベクトルを生成する"""
    response = bedrock_client.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText": text})
    )
    body = json.loads(response["body"].read())
    return body["embedding"]


def search_documents(query_embedding):
    """OpenSearchでk-NN検索する（後でOpenSearch接続を追加）"""
    # OpenSearch接続はモジュール追加時に実装
    return []


def generate_answer(question, contexts):
    """Bedrock Claude 3で回答を生成する"""
    context_text = "\n\n".join([f"[出典: {c.get('source', 'unknown')}]\n{c.get('text', '')}" for c in contexts])

    prompt = f"""以下のドキュメントを参考に、質問に答えてください。
ドキュメントに記載がない場合は「ドキュメントに該当する情報がありません」と答えてください。
必ず出典を明記してください。

ドキュメント:
{context_text}

質問: {question}

回答:"""

    response = bedrock_client.invoke_model(
        modelId="anthropic.claude-3-haiku-20240307-v1:0",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}]
        })
    )
    body = json.loads(response["body"].read())
    return body["content"][0]["text"]


def handler(event, context):
    """質問を受け取りRAGで回答する"""
    try:
        body = json.loads(event.get("body", "{}"))
        question = body.get("question", "")

        if not question:
            return {
                "statusCode": 400,
                "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": "質問が空です"})
            }

        # Embedding生成
        query_embedding = get_embedding(question)

        # 類似ドキュメント検索
        contexts = search_documents(query_embedding)

        # 回答生成
        answer = generate_answer(question, contexts)

        return {
            "statusCode": 200,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({
                "answer": answer,
                "sources": [c.get("source") for c in contexts]
            }, ensure_ascii=False)
        }

    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": str(e)})
        }