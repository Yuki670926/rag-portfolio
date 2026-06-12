"""authorizer ハンドラの不変条件。

重点：
- api_wildcard：メソッド限定 ARN をステージ全体のワイルドカードへ（キャッシュ汚染防止の核心）
- token_use=="id" 以外は Deny（access トークン等の流用拒否）
- Admin グループのみ Allow（役割で分けた認可）
"""


# ---------- api_wildcard / generate_policy（純ロジック） ----------

def test_api_wildcard(authorizer_h):
    arn = "arn:aws:execute-api:ap-northeast-1:123456789012:abc123/dev/POST/upload"
    assert authorizer_h.api_wildcard(arn) == \
        "arn:aws:execute-api:ap-northeast-1:123456789012:abc123/dev/*/*"


def test_generate_policy_shape(authorizer_h):
    p = authorizer_h.generate_policy("user", "Allow", "res")
    stmt = p["policyDocument"]["Statement"][0]
    assert stmt["Effect"] == "Allow"
    assert stmt["Action"] == "execute-api:Invoke"
    assert stmt["Resource"] == "res"


# ---------- handler（token_use / グループ検証） ----------

def _arn():
    return "arn:aws:execute-api:ap-northeast-1:123456789012:abc123/dev/POST/upload"


def test_denies_non_id_token(authorizer_h, monkeypatch):
    monkeypatch.setattr(authorizer_h, "get_jwks", lambda: {})
    monkeypatch.setattr(authorizer_h.jwt, "decode",
                        lambda *a, **k: {"token_use": "access", "cognito:groups": ["Admin"]})
    resp = authorizer_h.handler({"authorizationToken": "Bearer x", "methodArn": _arn()}, None)
    assert resp["policyDocument"]["Statement"][0]["Effect"] == "Deny"


def test_allows_admin_id_token_with_wildcard(authorizer_h, monkeypatch):
    monkeypatch.setattr(authorizer_h, "get_jwks", lambda: {})
    monkeypatch.setattr(authorizer_h.jwt, "decode",
                        lambda *a, **k: {"token_use": "id", "cognito:groups": ["Admin"]})
    monkeypatch.setattr(authorizer_h, "ALLOWED_GROUP", "Admin")
    resp = authorizer_h.handler({"authorizationToken": "Bearer x", "methodArn": _arn()}, None)
    stmt = resp["policyDocument"]["Statement"][0]
    assert stmt["Effect"] == "Allow"
    assert stmt["Resource"].endswith("/*/*")  # ワイルドカード化


def test_denies_non_admin(authorizer_h, monkeypatch):
    monkeypatch.setattr(authorizer_h, "get_jwks", lambda: {})
    monkeypatch.setattr(authorizer_h.jwt, "decode",
                        lambda *a, **k: {"token_use": "id", "cognito:groups": ["User"]})
    monkeypatch.setattr(authorizer_h, "ALLOWED_GROUP", "Admin")
    resp = authorizer_h.handler({"authorizationToken": "Bearer x", "methodArn": _arn()}, None)
    assert resp["policyDocument"]["Statement"][0]["Effect"] == "Deny"


def test_jwt_error_denies(authorizer_h, monkeypatch):
    from jose import JWTError
    monkeypatch.setattr(authorizer_h, "get_jwks", lambda: {})

    def boom(*a, **k):
        raise JWTError("bad signature")

    monkeypatch.setattr(authorizer_h.jwt, "decode", boom)
    resp = authorizer_h.handler({"authorizationToken": "Bearer x", "methodArn": _arn()}, None)
    assert resp["policyDocument"]["Statement"][0]["Effect"] == "Deny"
