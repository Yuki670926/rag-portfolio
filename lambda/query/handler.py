import json
import os
import boto3

bedrock_client = boto3.client("bedrock-runtime", region_name="ap-northeast-1")

OPENSEARCH_ENDPOINT = os.environ.get("OPENSEARCH_ENDPOINT", "")
INDEX_NAME = "documents"
TOP_K = 3


def get_embedding(text):
    response = bedrock_client.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText": text})
    )
    body = json.loads(response["body"].read())
    return body["embedding"]


def generate_answer(question, contexts):
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
    try:
        body = json.loads(event.get("body", "{}"))
        question = body.get("question", "")
        if not question:
            return {
                "statusCode": 400,
                "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": "質問が空です"})
            }
        query_embedding = get_embedding(question)
        contexts = []
        answer = generate_answer(question, contexts)
        return {
            "statusCode": 200,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"answer": answer, "sources": []}, ensure_ascii=False)
        }
    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": str(e)})
        }
