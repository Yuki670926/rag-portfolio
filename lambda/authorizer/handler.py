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
            audience=APP_CLIENT_ID
        )

        # グループを確認
        groups = claims.get("cognito:groups", [])

        if ALLOWED_GROUP in groups:
            return generate_policy("user", "Allow", method_arn)
        else:
            return generate_policy("user", "Deny", method_arn)

    except JWTError as e:
        print(f"JWT Error: {str(e)}")
        return generate_policy("user", "Deny", method_arn)
    except Exception as e:
        print(f"Error: {str(e)}")
        return generate_policy("user", "Deny", method_arn)

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