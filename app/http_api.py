"""platform-agent-adaptor HTTP API（标准库实现）。

只负责鉴权、参数校验，并把请求转交 `AgentAdaptorService`；
不直接调用模型或 GitHub。启用方式见 `ADAPTOR_API_*` 环境变量。
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import ssl
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("agent_adaptor.http_api")

MAX_BODY = 64 * 1024
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PATH_HANDLE = "/api/v1/issue/handle"
PATH_LATEST = "/api/v1/issue/tasks/latest"
PATH_KB_SEARCH = "/api/v1/kb/search"
PATH_KB_WIKI = "/api/v1/kb/wiki"
PATH_PUBLISH = "/api/v1/issue/publish"
MAX_TOP_K = 12
MAX_PUBLISH_ACTIONS = 10


class _BadRequest(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class RateLimiter:
    """进程内滑动窗口限流（单令牌场景足够，重启后清零）。"""

    def __init__(self, limit_per_minute: int):
        self.limit = max(1, int(limit_per_minute))
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._hits and now - self._hits[0] >= 60:
                self._hits.popleft()
            if len(self._hits) >= self.limit:
                return False
            self._hits.append(now)
            return True


class ScopedRateLimiter:
    """按 key（如 repo）分别计数的滑动窗口限流。"""

    def __init__(self, limit_per_minute: int):
        self.limit = max(1, int(limit_per_minute))
        self._entries: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._entries.setdefault(key, deque())
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


def _parse_repo(value: Any) -> str:
    if not isinstance(value, str):
        raise _BadRequest("repo 必须是字符串")
    repo = value.strip()
    if not REPO_RE.match(repo):
        raise _BadRequest("repo 格式应为 owner/repo")
    return repo


def _parse_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 10**9:
        raise _BadRequest("number 必须是不小于 1 的整数")
    return value


def _parse_text(value: Any, field: str, max_length: int, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str) or len(value) > max_length:
        raise _BadRequest(f"{field} 必须是不超过 {max_length} 字符的字符串")
    return value.strip()


def _parse_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _BadRequest(f"{field} 必须是 {minimum}~{maximum} 的整数")
    return value


class _Handler(BaseHTTPRequestHandler):
    server_version = "platform-agent-adaptor/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - 父类接口
        logger.debug("HTTP %s - %s", self.address_string(), fmt % args)

    # ---------------- 基础工具 ----------------
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {"error": code, "message": message})

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[len("Bearer "):].strip(), self.server.token)

    def _read_json(self) -> dict[str, Any]:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise _BadRequest("Content-Type 必须为 application/json")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            raise _BadRequest("Content-Length 不合法") from None
        if length <= 0 or length > MAX_BODY:
            raise _BadRequest(f"请求体长度必须在 1~{MAX_BODY} 字节之间")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _BadRequest("请求体不是合法 JSON") from None
        if not isinstance(payload, dict):
            raise _BadRequest("请求体必须是 JSON 对象")
        return payload

    def _check_repo_allowed(self, repo: str) -> None:
        if not self.server.repo_allowed(repo):
            raise _UnsupportedRepo(repo)

    # ---------------- 路由 ----------------
    def do_GET(self) -> None:  # noqa: N802 - 父类接口
        path = urlparse(self.path)
        if path.path == "/healthz":
            self._send_json(200, {"ok": True})
            return
        if not self._authorized():
            self._error(401, "UNAUTHORIZED", "缺少或错误的访问令牌")
            return
        if path.path == PATH_LATEST:
            self._get_latest(path)
            return
        if path.path == PATH_KB_WIKI:
            self._get_wiki(path)
            return
        self._error(404, "NOT_FOUND", "接口不存在")

    def _get_latest(self, path) -> None:
        try:
            query = parse_qs(path.query)
            repo = _parse_repo((query.get("repo") or [""])[0])
            raw_number = (query.get("number") or [""])[0].strip()
            if not raw_number.isdigit():
                raise _BadRequest("number 必须是不小于 1 的整数")
            number = _parse_number(int(raw_number))
            self._check_repo_allowed(repo)
        except _UnsupportedRepo as exc:
            self._error(422, "REPO_UNSUPPORTED", f"Issue Master 未托管该仓库：{exc.repo}")
            return
        except _BadRequest as exc:
            self._error(400, "INVALID_REQUEST", exc.message)
            return
        try:
            self._send_json(200, self.server.service.platform_task_status(repo, number))
        except Exception:  # noqa: BLE001 - 不向调用方泄漏堆栈
            logger.exception("平台 API 查询任务状态失败（%s#%s）", repo, number)
            self._error(500, "INTERNAL_ERROR", "查询任务状态失败，请查看 issue_master 日志")

    def _get_wiki(self, path) -> None:
        try:
            query = parse_qs(path.query)
            repo = _parse_repo((query.get("repo") or [""])[0])
            topic = _parse_text((query.get("topic") or [""])[0], "topic", 200)
            if not topic:
                raise _BadRequest("topic 不能为空")
            self._check_repo_allowed(repo)
        except _UnsupportedRepo as exc:
            self._error(422, "REPO_UNSUPPORTED", f"Issue Master 未托管该仓库：{exc.repo}")
            return
        except _BadRequest as exc:
            self._error(400, "INVALID_REQUEST", exc.message)
            return
        try:
            result = self.server.service.kb_wiki(repo, topic)
        except ValueError as exc:
            self._error(422, "REPO_UNSUPPORTED", str(exc))
            return
        except Exception:  # noqa: BLE001
            logger.exception("知识库 wiki 读取失败（%s / %s）", repo, topic)
            self._error(502, "KB_UNAVAILABLE", "知识库不可用，请查看 issue_master 与 deepwiki 日志")
            return
        if not str(result.get("text") or "").strip():
            self._error(404, "KB_WIKI_NOT_FOUND", f"未找到 wiki 页面：{topic}")
            return
        self._send_json(200, result)

    def do_POST(self) -> None:  # noqa: N802 - 父类接口
        path = urlparse(self.path)
        if not self._authorized():
            self._error(401, "UNAUTHORIZED", "缺少或错误的访问令牌")
            return
        if path.path == PATH_HANDLE:
            self._post_handle()
            return
        if path.path == PATH_KB_SEARCH:
            self._post_kb_search()
            return
        if path.path == PATH_PUBLISH:
            self._post_publish()
            return
        self._error(404, "NOT_FOUND", "接口不存在")

    def _post_handle(self) -> None:
        if not self.server.limiter.allow():
            self._error(429, "RATE_LIMITED", "请求过于频繁，请稍后重试")
            return
        try:
            payload = self._read_json()
            repo = _parse_repo(payload.get("repo"))
            number = _parse_number(payload.get("number"))
            hint = _parse_text(payload.get("hint"), "hint", 500)
            requester = _parse_text(payload.get("requester"), "requester", 120, "platform")
            self._check_repo_allowed(repo)
        except _UnsupportedRepo as exc:
            self._error(422, "REPO_UNSUPPORTED", f"Issue Master 未托管该仓库：{exc.repo}")
            return
        except _BadRequest as exc:
            self._error(400, "INVALID_REQUEST", exc.message)
            return
        try:
            task_id = self.server.service.start_from_platform(
                repo, number, requester or "platform", hint
            )
        except ValueError as exc:
            self._error(400, "INVALID_REQUEST", str(exc))
            return
        except Exception:  # noqa: BLE001 - 不向调用方泄漏堆栈
            logger.exception("平台 API 创建处理任务失败（%s#%s）", repo, number)
            self._error(500, "INTERNAL_ERROR", "创建处理任务失败，请查看 issue_master 日志")
            return
        logger.info("平台触发处理任务：%s#%s（task=%s, requester=%s）", repo, number, task_id, requester)
        self._send_json(
            202,
            {"taskId": int(task_id), "status": "queued", "repo": repo, "number": number},
        )

    def _post_kb_search(self) -> None:
        try:
            payload = self._read_json()
            repo = _parse_repo(payload.get("repo"))
            question = _parse_text(payload.get("question"), "question", 2000)
            if not question:
                raise _BadRequest("question 不能为空")
            top_k = _parse_int(payload.get("topK", 8), "topK", 1, MAX_TOP_K)
            self._check_repo_allowed(repo)
        except _UnsupportedRepo as exc:
            self._error(422, "REPO_UNSUPPORTED", f"Issue Master 未托管该仓库：{exc.repo}")
            return
        except _BadRequest as exc:
            self._error(400, "INVALID_REQUEST", exc.message)
            return
        if not self.server.search_limiter.allow(repo.lower()):
            self._error(429, "RATE_LIMITED", "该仓库检索过于频繁，请稍后重试")
            return
        try:
            data = self.server.service.kb_search(repo, question, top_k)
        except ValueError as exc:
            self._error(422, "REPO_UNSUPPORTED", str(exc))
            return
        except Exception:  # noqa: BLE001 - 知识库异常如实回报 502
            logger.exception("知识库检索失败（%s）", repo)
            self._error(502, "KB_UNAVAILABLE", "知识库不可用，请查看 issue_master 与 deepwiki 日志")
            return
        self._send_json(200, data)

    def _post_publish(self) -> None:
        try:
            payload = self._read_json()
            repo = _parse_repo(payload.get("repo"))
            number = _parse_number(payload.get("number"))
            comment_md = _parse_text(payload.get("comment_md"), "comment_md", 20000)
            post_comment = payload.get("post_comment", True)
            if not isinstance(post_comment, bool):
                raise _BadRequest("post_comment 必须是布尔值")
            if post_comment and not comment_md:
                raise _BadRequest("comment_md 不能为空")
            actions = payload.get("actions") or []
            if not isinstance(actions, list) or len(actions) > MAX_PUBLISH_ACTIONS:
                raise _BadRequest(f"actions 必须是不超过 {MAX_PUBLISH_ACTIONS} 项的数组")
            requester = _parse_text(payload.get("requester"), "requester", 120, "agent")
            key = _parse_text(
                payload.get("idempotencyKey", payload.get("idempotency_key")),
                "idempotencyKey",
                200,
            )
            self._check_repo_allowed(repo)
        except _UnsupportedRepo as exc:
            self._error(422, "REPO_UNSUPPORTED", f"Issue Master 未托管该仓库：{exc.repo}")
            return
        except _BadRequest as exc:
            self._error(400, "INVALID_REQUEST", exc.message)
            return
        try:
            result = self.server.service.publish_external(
                repo,
                number,
                comment_md,
                actions,
                requester=requester or "agent",
                idempotency_key=key,
                post_comment=post_comment,
            )
        except ValueError as exc:
            self._error(400, "INVALID_REQUEST", str(exc))
            return
        except PermissionError as exc:
            self._error(403, "FORBIDDEN", str(exc))
            return
        except Exception:  # noqa: BLE001 - 不向调用方泄漏堆栈
            logger.exception("外部草稿发布失败（%s#%s）", repo, number)
            self._error(500, "INTERNAL_ERROR", "发布失败，请查看 issue_master 日志")
            return
        logger.info("外部草稿已发布：%s#%s（requester=%s）", repo, number, requester)
        self._send_json(200, result)


class _UnsupportedRepo(Exception):
    def __init__(self, repo: str):
        super().__init__(repo)
        self.repo = repo


class IssueMasterHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        service: Any,
        token: str,
        repo_allowed: Callable[[str], bool],
        rate_limit_per_minute: int,
        search_rate_limit_per_minute: int = 30,
    ):
        self.service = service
        self.token = token
        self.repo_allowed = repo_allowed
        self.limiter = RateLimiter(rate_limit_per_minute)
        self.search_limiter = ScopedRateLimiter(search_rate_limit_per_minute)
        super().__init__(address, _Handler)


def create_http_server(
    service: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8091,
    token: str,
    repo_allowed: Callable[[str], bool],
    rate_limit_per_minute: int = 30,
    search_rate_limit_per_minute: int = 30,
    tls_cert: str = "",
    tls_key: str = "",
) -> IssueMasterHTTPServer:
    """构建（尚未 start）的 HTTP 服务；token 为空时直接拒绝。"""
    if not token:
        raise ValueError("ISSUE_MASTER_API_TOKEN 不能为空")
    server = IssueMasterHTTPServer(
        (host, int(port)),
        service=service,
        token=token,
        repo_allowed=repo_allowed,
        rate_limit_per_minute=rate_limit_per_minute,
        search_rate_limit_per_minute=search_rate_limit_per_minute,
    )
    if tls_cert or tls_key:
        if not (tls_cert and tls_key):
            server.server_close()
            raise ValueError("TLS 证书与私钥必须同时配置")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls_cert, tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def start_in_thread(server: ThreadingHTTPServer, name: str = "issue-master-api") -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
    thread.start()
    return thread
