import json
import os
import urllib.request
from jose import jwt, JWTError

REGION = os.environ.get("REGION", "ap-northeast-1")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
APP_CLIENT_ID = os.environ.get("APP_CLIENT_ID", "")
ALLOWED_GROUP = os.environ.get("ALLOWED_GROUP", "Admin")

JWKS_URL = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}/.well-known/jwks.json"

def get_jwks():
    with urllib.request.urlopen(JWKS_URL) as response:
        return json.loads(response.read())

def handler(event, context):
    token = event.get("authorizationToken", "")
    method_arn = event.get("methodArn", "")

    try:
        # BearerトークンからJWTを取り出す
        if token.startswith("Bearer "):
            token = token[7:]

        # CognitoのJWKS（公開鍵）を取得
        jwks = get_jwks()

        # JWTを署名検証付きでデコード
        claims = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            audience=APP_CLIENT_ID,
            issuer=f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}",
        )

        # id トークンのみ受け付ける（access トークン等の流用を拒否）
        if claims.get("token_use") != "id":
            print(f"authz: DENY (token_use={claims.get('token_use')}) methodArn={method_arn}")
            return generate_policy("user", "Deny", method_arn)

        # グループを確認
        groups = claims.get("cognito:groups", [])

        if ALLOWED_GROUP in groups:
            # TOKEN authorizer の結果はトークン単位でキャッシュされる。特定メソッド ARN を
            # 返すと、先に許可したメソッド(例 POST /upload)のポリシーが他メソッド(例 GET /status)
            # に流用され 403 になる。ステージ全メソッドを許可するワイルドカードを返し、
            # キャッシュ(TTL=300)を安全に共有する。
            return generate_policy("user", "Allow", api_wildcard(method_arn))
        else:
            print(f"authz: DENY (group missing) methodArn={method_arn} groups={groups}")
            return generate_policy("user", "Deny", method_arn)

    except JWTError as e:
        print(f"authz: JWT Error: {str(e)} methodArn={method_arn}")
        return generate_policy("user", "Deny", method_arn)
    except Exception as e:
        print(f"Error: {str(e)}")
        return generate_policy("user", "Deny", method_arn)

def api_wildcard(method_arn):
    # methodArn = arn:aws:execute-api:region:acct:apiId/stage/METHOD/path...
    # → arn:aws:execute-api:region:acct:apiId/stage/*/*（ステージ内の全メソッド/全リソース）
    try:
        parts = method_arn.split("/")
        return f"{parts[0]}/{parts[1]}/*/*"
    except Exception:
        return method_arn


def generate_policy(principal_id, effect, resource):
    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource
                }
            ]
        }
    }