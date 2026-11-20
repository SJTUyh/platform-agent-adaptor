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
        self.status = {
            "task": {
                "id": 7,
                "status": "consumed",
                "createdAt": "2026-11-17T10:00:00",
                "requester": "platform:alice",
            },
            "draft": {
                "status": "pending_confirm",
                "commentUrl": "",
                "updatedAt": "2026-11-17T10:01:00",
            },
            "feishuNotifyReady": True,
        }

    def is_supported_repo(self, repo: str) -> bool:
        return repo.strip().lower() == "aisbench/benchmark"

    def start_from_platform(self, repo: str, number: int, requester: str, hint: str) -> int:
        self.calls.append((repo, number, requester, hint))
        return 7

    def platform_task_status(self, repo: str, number: int) -> dict:
        return self.status


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
        self.assertEqual(self.call("/api/v1/issue/tasks/latest?repo=AISBench/benchmark&number=1", token="")[0], 401)
        self.assertEqual(self.call("/api/v1/issue/tasks/latest?repo=AISBench/benchmark&number=1", token="bad")[0], 401)

    def test_trigger_queues_task(self):
        status, body = self.call(
            "/api/v1/issue/handle",
            method="POST",
            body={"repo": "AISBench/benchmark", "number": 12, "hint": "看看复现步骤", "requester": "platform:alice"},
        )
        self.assertEqual(status, 202)
        self.assertEqual(body["taskId"], 7)
        self.assertEqual(body["status"], "queued")
        self.assertEqual(self.service.calls, [("AISBench/benchmark", 12, "platform:alice", "看看复现步骤")])

    def test_trigger_validation(self):
        self.assertEqual(self.call("/api/v1/issue/handle", method="POST", raw=b"{bad json")[0], 400)
        self.assertEqual(self.call("/api/v1/issue/handle", method="POST", body={"repo": "AISBench/benchmark"})[0], 400)
        self.assertEqual(
            self.call("/api/v1/issue/handle", method="POST", body={"repo": "AISBench/benchmark", "number": 1, "hint": "x" * 501})[0],
            400,
        )
        self.assertEqual(
            self.call("/api/v1/issue/handle", method="POST", body={"repo": "other/repo", "number": 1})[0],
            422,
        )
        self.assertEqual(self.service.calls, [])

    def test_status_query(self):
        status, body = self.call("/api/v1/issue/tasks/latest?repo=AISBench/benchmark&number=12")
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["status"], "consumed")
        self.assertEqual(body["draft"]["status"], "pending_confirm")
        self.assertTrue(body["feishuNotifyReady"])
        self.assertEqual(
            self.call("/api/v1/issue/tasks/latest?repo=AISBench/benchmark&number=abc")[0], 400
        )
        self.assertEqual(
            self.call("/api/v1/issue/tasks/latest?repo=other/repo&number=1")[0], 422
        )

    def test_unknown_path(self):
        self.assertEqual(self.call("/api/v1/nope")[0], 404)
        self.assertEqual(self.call("/api/v1/nope", method="POST", body={})[0], 404)


class RateLimitTest(unittest.TestCase):
    rate_limit = 2

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
        start_in_thread(self.server)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _post(self):
        req = urlrequest.Request(
            self.base + "/api/v1/issue/handle",
            data=json.dumps({"repo": "AISBench/benchmark", "number": 1}).encode(),
            method="POST",
        )
        req.add_header("Authorization", "Bearer secret-token")
        req.add_header("Content-Type", "application/json")
        try:
            with urlrequest.urlopen(req, timeout=5) as resp:
                return resp.status
        except HTTPError as exc:
            return exc.code

    def test_excess_requests_are_limited(self):
        self.assertEqual([self._post() for _ in range(3)], [202, 202, 429])


if __name__ == "__main__":
    unittest.main()
