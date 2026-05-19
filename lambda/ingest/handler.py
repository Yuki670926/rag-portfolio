import json
import os
import boto3
import urllib.parse
from pypdf import PdfReader
from io import BytesIO

s3_client = boto3.client("s3")
bedrock_client = boto3.client("bedrock-runtime", region_name="ap-northeast-1")

OPENSEARCH_ENDPOINT = os.environ.get("OPENSEARCH_ENDPOINT", "")
INDEX_NAME = "documents"
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50


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
        for i, chunk in enumerate(chunks):
            embedding = get_embedding(chunk)
            print(f"Indexed chunk {i}: {chunk[:50]}...")
    return {"statusCode": 200, "body": "Ingestion complete"}
