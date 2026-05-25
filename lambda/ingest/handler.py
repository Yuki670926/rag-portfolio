import json
import os
import boto3
import urllib.parse
from pypdf import PdfReader
from io import BytesIO
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

s3_client = boto3.client("s3")
bedrock_client = boto3.client("bedrock-runtime", region_name="ap-northeast-1")
ssm_client = boto3.client("ssm", region_name="ap-northeast-1")

VECTOR_STORE_TYPE = os.environ.get("VECTOR_STORE_TYPE", "opensearch")
SSM_ENDPOINT_PARAM = os.environ.get("SSM_ENDPOINT_PARAM", "")
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
        print(f"SSM error: {str(e)}")
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
        connection_class=RequestsHttpConnection
    )

def ensure_index(client):
    if not client.indices.exists(INDEX_NAME):
        client.indices.create(
            index=INDEX_NAME,
            body={
                "settings": {"index.knn": True},
                "mappings": {
                    "properties": {
                        "embedding": {
                            "type": "knn_vector",
                            "dimension": 1024
                        },
                        "text": {"type": "text"},
                        "source": {"type": "keyword"},
                        "chunk_index": {"type": "integer"}
                    }
                }
            }
        )
        print(f"Index {INDEX_NAME} created")

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

def handler(event, context):
    if VECTOR_STORE_TYPE == "opensearch":
        endpoint = get_vector_store_endpoint()
        if not endpoint:
            print("OpenSearch endpoint not found in SSM")
            return {"statusCode": 500, "body": "OpenSearch endpoint not configured"}
        os_client = get_opensearch_client(endpoint)
    elif VECTOR_STORE_TYPE == "s3_vectors":
        # S3 Vectors実装予定（17番）
        print("S3 Vectors is not implemented yet")
        return {"statusCode": 200, "body": "S3 Vectors not implemented"}
    else:
        print(f"Unknown vector store type: {VECTOR_STORE_TYPE}")
        return {"statusCode": 500, "body": "Unknown vector store type"}

    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        print(f"Processing: s3://{bucket}/{key}")

        response = s3_client.get_object(Bucket=bucket, Key=key)
        pdf_bytes = response["Body"].read()
        reader = PdfReader(BytesIO(pdf_bytes))
        full_text = ""
        for page in reader.pages:
            full_text += page.extract_text() + "\n"

        chunks = chunk_text(full_text)
        print(f"Total chunks: {len(chunks)}")

        ensure_index(os_client)
        for i, chunk in enumerate(chunks):
            embedding = get_embedding(chunk)
            doc = {
                "text": chunk,
                "embedding": embedding,
                "source": key,
                "chunk_index": i
            }
            os_client.index(index=INDEX_NAME, body=doc)
            print(f"Indexed chunk {i}: {chunk[:50]}...")

    return {"statusCode": 200, "body": "Ingestion complete"}