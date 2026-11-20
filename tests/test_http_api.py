"""平台接入 HTTP API 的单元测试（标准库，不依赖飞书/GitHub/模型）。"""
from __future__ import annotations

import json
import unittest
from urllib import request as urlrequest
from urllib.error import HTTPError

from app.http_api import create_http_server, start_in_thread


class FakeService:
    def __init__(self):
        self.calls: list[tuple] = []

    def is_supported_repo(self, repo: str) -> bool:
        return repo.strip().lower() == "aisbench/benchmark"


class HttpApiTest(unittest.TestCase):
    rate_limit = 30

    def setUp(self):
        self.service = FakeService()
        self.server = create_http_server(
            self.service,
            host="127.0.0.1",
            port=0,
            token="secret-token",
            repo_allowed=self.service.is_supported_repo,
            rate_limit_per_minute=self.rate_limit,
        )
        self.thread = start_in_thread(self.server)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, path, method="GET", body=None, token="secret-token", raw=None):
        data = raw if raw is not None else (
            json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        )
        req = urlrequest.Request(self.base + path, data=data, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urlrequest.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_health_needs_no_token(self):
        status, body = self.call("/healthz", token="")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})

    def test_auth_required(self):
        self.assertEqual(self.call("/api/v1/kb/wiki?repo=AISBench/benchmark&topic=x", token="")[0], 401)
        self.assertEqual(self.call("/api/v1/kb/wiki?repo=AISBench/benchmark&topic=x", token="bad")[0], 401)

    def test_internal_trigger_endpoints_are_gone(self):
        self.assertEqual(self.call("/api/v1/issue/handle", method="POST", body={})[0], 404)
        self.assertEqual(
            self.call("/api/v1/issue/tasks/latest?repo=AISBench/benchmark&number=1")[0], 404
        )

    def test_unknown_path(self):
        self.assertEqual(self.call("/api/v1/nope")[0], 404)
        self.assertEqual(self.call("/api/v1/nope", method="POST", body={})[0], 404)


if __name__ == "__main__":
    unittest.main()
