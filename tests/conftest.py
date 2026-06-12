"""テスト共通設定。

各 Lambda ハンドラはファイル名が一律 `handler.py`（モジュール名が衝突する）ため、
importlib で「query_handler / ingest_handler …」と別名でロードしてフィクスチャ提供する。
ハンドラはトップレベルで boto3 クライアントや Powertools を生成するが、
client/resource の生成自体は認証不要（実 API 呼び出し時に初めて要る）なので import は通る。
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

# Powertools をローカルで静かに動かす（メトリクス名前空間・トレース無効化）。import より前に設定する。
os.environ.setdefault("POWERTOOLS_METRICS_NAMESPACE", "test")
os.environ.setdefault("POWERTOOLS_SERVICE_NAME", "test")
os.environ.setdefault("POWERTOOLS_TRACE_DISABLED", "true")

LAMBDA_DIR = Path(__file__).resolve().parent.parent / "lambda"


def _load(module_name: str, rel_path: str):
    path = LAMBDA_DIR / rel_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def query_h():
    return _load("query_handler", "query/handler.py")


@pytest.fixture(scope="session")
def ingest_h():
    return _load("ingest_handler", "ingest/handler.py")


@pytest.fixture(scope="session")
def presigned_h():
    return _load("presigned_handler", "presigned_url/handler.py")


@pytest.fixture(scope="session")
def authorizer_h():
    return _load("authorizer_handler", "authorizer/handler.py")


@pytest.fixture
def lambda_context():
    """Powertools の inject_lambda_context が要求する属性を持つダミー context。"""
    class Ctx:
        function_name = "test"
        function_version = "$LATEST"
        invoked_function_arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:test"
        memory_limit_in_mb = 128
        aws_request_id = "test-request-id"
        log_group_name = "/aws/lambda/test"
        log_stream_name = "2026/06/11/[$LATEST]abcdef"

        def get_remaining_time_in_millis(self):
            return 30000

    return Ctx()
