"""平台 Agent 接入 API 与外部发布的测试（标准库，不依赖 deepwiki / GitHub / 模型）。"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from urllib import request as urlrequest
from urllib.error import HTTPError

from app.http_api import create_http_server, start_in_thread
from engine.publish import ExternalPublishEngine
from store.db import Database


class FakeService:
    def __init__(self):
        self.search_calls: list[tuple] = []
        self.wiki_calls: list[tuple] = []
        self.wiki_text: str = "# page"
        self.publish_calls: list[tuple] = []
        self.published: dict[str, dict] = {}

    def is_supported_repo(self, repo: str) -> bool:
        return repo.strip().lower() == "aisbench/benchmark"

    def kb_search(self, repo: str, question: str, top_k: int = 8) -> dict:
        self.search_calls.append((repo, question, top_k))
        return {
            "repo": repo,
            "chunks": [{"path": "tools/config.py", "startLine": 1, "endLine": 2, "title": "tools/config.py", "text": "x", "score": 0.5}],
            "index": {"status": "fresh", "sha": "abc"},
            "warning": "",
        }

    def kb_wiki(self, repo: str, topic: str) -> dict:
        self.wiki_calls.append((repo, topic))
        return {"repo": repo, "topic": topic, "text": self.wiki_text}

    def publish_external(self, repo, number, comment_md, actions, requester="", idempotency_key="", post_comment=True):
        self.publish_calls.append((repo, number, comment_md, actions, requester, idempotency_key, post_comment))
        if idempotency_key and idempotency_key in self.published:
            return {**self.published[idempotency_key], "replayed": True}
        for action in actions:
            if action.get("kind") not in ("add_label", "remove_label", "assign", "close", "reopen"):
                raise ValueError(f"不支持的动作：{action.get('kind')}")
        result = {
            "commentUrl": "https://github.com/AISBench/benchmark/issues/1#issuecomment-9" if post_comment else "",
            "applied": (["发表评论"] if post_comment else []) + [f"{action.get('kind')}" for action in actions],
            "errors": [],
            "replayed": False,
        }
        if idempotency_key:
            self.published[idempotency_key] = result
        return result


class HttpApiBase(unittest.TestCase):
    rate_limit = 30
    search_rate_limit = 30

    def setUp(self):
        self.service = FakeService()
        self.server = create_http_server(
            self.service,
            host="127.0.0.1",
            port=0,
            token="secret-token",
            repo_allowed=self.service.is_supported_repo,
            rate_limit_per_minute=self.rate_limit,
            search_rate_limit_per_minute=self.search_rate_limit,
        )
        start_in_thread(self.server)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, path, method="GET", body=None, token="secret-token"):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
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


class KbSearchTest(HttpApiBase):
    def test_search_returns_chunks(self):
        status, body = self.call(
            "/api/v1/kb/search",
            method="POST",
            body={"repo": "AISBench/benchmark", "question": "配置在哪", "topK": 5},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["chunks"][0]["path"], "tools/config.py")
        self.assertEqual(body["index"]["status"], "fresh")
        self.assertEqual(self.service.search_calls, [("AISBench/benchmark", "配置在哪", 5)])

    def test_search_defaults_and_validation(self):
        status, _ = self.call("/api/v1/kb/search", method="POST", body={"repo": "AISBench/benchmark", "question": "x"})
        self.assertEqual(status, 200)
        self.assertEqual(self.service.search_calls[-1][2], 8)
        cases = [
            {"repo": "AISBench/benchmark"},
            {"repo": "AISBench/benchmark", "question": ""},
            {"repo": "AISBench/benchmark", "question": "x", "topK": 13},
            {"repo": "AISBench/benchmark", "question": "x", "topK": "8"},
            {"repo": "other/repo", "question": "x"},
        ]
        statuses = [self.call("/api/v1/kb/search", method="POST", body=case)[0] for case in cases]
        self.assertEqual(statuses, [400, 400, 400, 400, 422])

    def test_search_requires_token(self):
        self.assertEqual(self.call("/api/v1/kb/search", method="POST", body={}, token="")[0], 401)
        self.assertEqual(self.call("/api/v1/kb/search", method="POST", body={}, token="bad")[0], 401)

    def test_wiki(self):
        status, body = self.call("/api/v1/kb/wiki?repo=AISBench/benchmark&topic=eval")
        self.assertEqual(status, 200)
        self.assertEqual(body["text"], "# page")
        self.assertEqual(self.service.wiki_calls, [("AISBench/benchmark", "eval")])
        self.assertEqual(self.call("/api/v1/kb/wiki?repo=AISBench/benchmark")[0], 400)
        self.assertEqual(self.call("/api/v1/kb/wiki?repo=other/repo&topic=x")[0], 422)

    def test_wiki_not_found(self):
        self.service.wiki_text = ""
        status, body = self.call("/api/v1/kb/wiki?repo=AISBench/benchmark&topic=missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "KB_WIKI_NOT_FOUND")

    def test_body_limit_allows_long_comments(self):
        # MAX_BODY is 64KB now; the old 16KB limit would reject this valid request.
        long_question = "x" * 2000
        status, _ = self.call("/api/v1/kb/search", method="POST", body={"repo": "AISBench/benchmark", "question": long_question})
        self.assertEqual(status, 200)


class PublishTest(HttpApiBase):
    def payload(self, **overrides):
        body = {
            "repo": "AISBench/benchmark",
            "number": 1,
            "comment_md": "机器人评论",
            "actions": [{"kind": "add_label", "value": "bug", "reason": "复现"}],
            "requester": "agent:alice",
            "idempotencyKey": "draft-1",
        }
        body.update(overrides)
        return body

    def test_publish_and_idempotent_replay(self):
        status, body = self.call("/api/v1/issue/publish", method="POST", body=self.payload())
        self.assertEqual(status, 200)
        self.assertEqual(body["commentUrl"], "https://github.com/AISBench/benchmark/issues/1#issuecomment-9")
        self.assertFalse(body["replayed"])
        status, replay = self.call("/api/v1/issue/publish", method="POST", body=self.payload())
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.service.publish_calls), 2)

    def test_actions_only_can_skip_the_comment(self):
        status, body = self.call(
            "/api/v1/issue/publish",
            method="POST",
            body=self.payload(comment_md="", post_comment=False),
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["commentUrl"], "")
        self.assertFalse(self.service.publish_calls[-1][-1])

    def test_publish_validation(self):
        self.assertEqual(self.call("/api/v1/issue/publish", method="POST", body=self.payload(comment_md=""))[0], 400)
        self.assertEqual(self.call("/api/v1/issue/publish", method="POST", body=self.payload(repo="other/repo"))[0], 422)
        self.assertEqual(self.call("/api/v1/issue/publish", method="POST", body=self.payload(actions=[{"kind": "nope"} for _ in range(11)]))[0], 400)
        self.assertEqual(
            self.call("/api/v1/issue/publish", method="POST", body=self.payload(actions=[{"kind": "delete_repo"}]))[0],
            400,
        )
        self.assertEqual(self.call("/api/v1/issue/publish", method="POST", body={"repo": "AISBench/benchmark"})[0], 400)


class SearchRateLimitTest(HttpApiBase):
    search_rate_limit = 1

    def test_search_is_rate_limited_per_repo(self):
        body = {"repo": "AISBench/benchmark", "question": "x"}
        self.assertEqual(self.call("/api/v1/kb/search", method="POST", body=body)[0], 200)
        self.assertEqual(self.call("/api/v1/kb/search", method="POST", body=body)[0], 429)


class FakeGitHub:
    def __init__(self):
        self.comments: list[tuple] = []
        self.actions: list[tuple] = []

    def post_comment(self, repo, number, body):
        self.comments.append((repo, number, body))
        return {"html_url": f"https://github.com/{repo}/issues/{number}#issuecomment-1"}

    def add_labels(self, repo, number, labels):
        self.actions.append(("add_label", tuple(labels)))

    def remove_label(self, repo, number, label):
        self.actions.append(("remove_label", label))

    def assign(self, repo, number, users):
        self.actions.append(("assign", tuple(users)))

    def close_issue(self, repo, number):
        self.actions.append(("close", number))

    def reopen_issue(self, repo, number):
        self.actions.append(("reopen", number))


class ExternalPublishEngineTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="agent-adaptor-publish-")
        self.path = os.path.join(self.directory, "test.sqlite")
        self.db = Database(self.path)
        self.github = FakeGitHub()
        self.engine = ExternalPublishEngine(self.db, self.github)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.directory, ignore_errors=True)

    def test_publish_is_idempotent_and_audited(self):
        first = self.engine.publish_external(
            "AISBench/benchmark", 1, "评论正文",
            [{"kind": "add_label", "value": "bug", "reason": "复现"}],
            requester="agent:alice", idempotency_key="draft-1",
        )
        self.assertFalse(first.replayed)
        self.assertEqual(len(self.github.comments), 1)
        self.assertEqual(self.github.actions, [("add_label", ("bug",))])
        second = self.engine.publish_external(
            "AISBench/benchmark", 1, "评论正文",
            [{"kind": "add_label", "value": "bug", "reason": "复现"}],
            requester="agent:alice", idempotency_key="draft-1",
        )
        self.assertTrue(second.replayed)
        self.assertEqual(len(self.github.comments), 1, "replay must not post twice")
        conn = sqlite3.connect(self.path)
        try:
            row = conn.execute("SELECT action, target FROM audit_log WHERE action='issue.publish.external'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ("issue.publish.external", "AISBench/benchmark#1"))

    def test_actions_only_skips_the_comment(self):
        result = self.engine.publish_external(
            "AISBench/benchmark", 1, "",
            [{"kind": "add_label", "value": "bug", "reason": "复现"}],
            requester="agent:alice", idempotency_key="draft-actions", post_comment=False,
        )
        self.assertEqual(self.github.comments, [])
        self.assertEqual(self.github.actions, [("add_label", ("bug",))])
        self.assertEqual(result.applied, ["添加标签：bug"])

    def test_invalid_actions_are_rejected_before_posting(self):
        with self.assertRaises(ValueError):
            self.engine.publish_external("AISBench/benchmark", 1, "x", [{"kind": "delete_repo", "value": "x"}])
        with self.assertRaises(ValueError):
            self.engine.publish_external("AISBench/benchmark", 1, "")
        with self.assertRaises(ValueError):
            self.engine.publish_external("AISBench/benchmark", 1, "x", [{"kind": "add_label", "value": ""}])
        self.assertEqual(self.github.comments, [])


if __name__ == "__main__":
    unittest.main()
