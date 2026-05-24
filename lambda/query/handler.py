import json
import os
import boto3
from datetime import datetime, timezone, timedelta
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

bedrock_client = boto3.client("bedrock-runtime", region_name="ap-northeast-1")
dynamodb = boto3.resource("dynamodb", region_name="ap-northeast-1")

OPENSEARCH_ENDPOINT = os.environ.get("OPENSEARCH_ENDPOINT", "")
CONVERSATIONS_TABLE = os.environ.get("CONVERSATIONS_TABLE", "")
SESSIONS_TABLE = os.environ.get("SESSIONS_TABLE", "")
INDEX_NAME = "documents"
TOP_K = 3
MAX_HISTORY = 5
TTL_DAYS = 90

def get_aws_auth():
    credentials = boto3.Session().get_credentials()
    return AWS4Auth(
        credentials.access_key,
        credentials.secret_key,
        "ap-northeast-1",
        "aoss",
        session_token=credentials.token
    )

def get_opensearch_client():
    host = OPENSEARCH_ENDPOINT.replace("https://", "")
    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=get_aws_auth(),
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection
    )

def get_embedding(text):
    response = bedrock_client.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText": text})
    )
    body = json.loads(response["body"].read())
    return body["embedding"]

def search_documents(query_embedding):
    if not OPENSEARCH_ENDPOINT:
        return []
    try:
        client = get_opensearch_client()
        query = {
            "size": TOP_K,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": query_embedding,
                        "k": TOP_K
                    }
                }
            }
        }
        response = client.search(index=INDEX_NAME, body=query)
        return [hit["_source"] for hit in response["hits"]["hits"]]
    except Exception as e:
        print(f"OpenSearch error: {str(e)}")
        return []

def get_session_id(user_id):
    try:
        table = dynamodb.Table(SESSIONS_TABLE)
        response = table.query(
            KeyConditionExpression="user_id = :uid",
            ExpressionAttributeValues={":uid": user_id},
            ScanIndexForward=False,
            Limit=1
        )
        items = response.get("Items", [])
        if items:
            return items[0]["session_id"]
        return None
    except Exception as e:
        print(f"DynamoDB session error: {str(e)}")
        return None

def get_conversation_history(user_id, session_id):
    if not session_id:
        return []
    try:
        table = dynamodb.Table(CONVERSATIONS_TABLE)
        response = table.query(
            KeyConditionExpression="user_id = :uid",
            ExpressionAttributeValues={":uid": user_id},
            ScanIndexForward=False,
            Limit=MAX_HISTORY
        )
        items = response.get("Items", [])
        # 古い順に並べ直す
        items.reverse()
        return items
    except Exception as e:
        print(f"DynamoDB history error: {str(e)}")
        return []

def save_conversation(user_id, session_id, question, answer):
    try:
        table = dynamodb.Table(CONVERSATIONS_TABLE)
        now = datetime.now(timezone.utc)
        ttl = int((now + timedelta(days=TTL_DAYS)).timestamp())
        table.put_item(Item={
            "user_id": user_id,
            "timestamp": now.isoformat(),
            "session_id": session_id,
            "question": question,
            "answer": answer,
            "ttl": ttl
        })
    except Exception as e:
        print(f"DynamoDB save error: {str(e)}")

def save_session(user_id, session_id):
    try:
        table = dynamodb.Table(SESSIONS_TABLE)
        now = datetime.now(timezone.utc)
        ttl = int((now + timedelta(days=TTL_DAYS)).timestamp())
        table.put_item(Item={
            "user_id": user_id,
            "last_accessed_at": now.isoformat(),
            "session_id": session_id,
            "ttl": ttl
        })
    except Exception as e:
        print(f"DynamoDB session save error: {str(e)}")

def generate_answer(question, contexts, history):
    context_text = "\n\n".join([
        f"[出典: {c.get('source', 'unknown')}]\n{c.get('text', '')}"
        for c in contexts
    ])

    history_text = "\n".join([
        f"ユーザー: {h['question']}\nアシスタント: {h['answer']}"
        for h in history
    ])

    if contexts:
        prompt = f"""以下のドキュメントを元に、質問に答えてください。
ドキュメントに情報がない場合は、「ドキュメントに該当する情報がありません」と答えてください。
必ず出典を明示してください。

ドキュメント:
{context_text}

過去の会話:
{history_text}

質問: {question}

回答:"""
    else:
        prompt = f"""過去の会話:
{history_text}

質問: {question}

回答:"""

    response = bedrock_client.invoke_model(
        modelId="jp.anthropic.claude-haiku-4-5-20251001-v1:0",
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

        # user_idをLambda Authorizerのcontextから取得
        user_id = event.get("requestContext", {}).get("authorizer", {}).get("user_id", "anonymous")

        # セッション管理
        session_id = get_session_id(user_id)
        if not session_id:
            session_id = f"{user_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

        # 会話履歴取得
        history = get_conversation_history(user_id, session_id)

        # RAG検索
        query_embedding = get_embedding(question)
        contexts = search_documents(query_embedding)

        # 回答生成
        answer = generate_answer(question, contexts, history)

        # 会話履歴・セッション保存
        save_conversation(user_id, session_id, question, answer)
        save_session(user_id, session_id)

        return {
            "statusCode": 200,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({
                "answer": answer,
                "sources": list(set([c.get("source") for c in contexts if c.get("source")])),
                "context_count": len(contexts)
            }, ensure_ascii=False)
        }
    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": str(e)})
        }