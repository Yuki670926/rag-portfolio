import os
import json
import boto3

# Cognito Post-Authentication トリガ。ログイン成功のたびに warmer（query Lambda）を
# 非同期(Event)起動し、OpenSearch Serverless(NextGen) collection を暖機する。
# scale-to-zero でアイドル後はコールドになるため、「ログイン→質問」までの人間の操作時間に
# 暖機を済ませておく狙い（cold-start 対策・選択肢 D）。
# 認証フローをブロック/失敗させてはならないので、必ず例外を握り潰し event をそのまま返す。

lambda_client = boto3.client("lambda", region_name=os.environ.get("AWS_REGION", "ap-northeast-1"))
WARMER_TARGET = os.environ.get("WARMER_TARGET", "")  # 暖機させる query Lambda の関数名


def handler(event, context):
    try:
        if WARMER_TARGET:
            lambda_client.invoke(
                FunctionName=WARMER_TARGET,
                InvocationType="Event",  # 非同期＝ログインを待たせない
                Payload=json.dumps({"warmup": True}).encode("utf-8"),
            )
    except Exception as e:
        print(f"postauth warmer invoke failed (ignored): {e}")
    return event  # Cognito トリガは event をそのまま返す必要がある
