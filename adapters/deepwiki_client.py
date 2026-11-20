"""自托管 deepwiki-open 知识库的 HTTP 客户端。

M0 只做连通性探测；M1 起扩展为完整的 SSE 客户端（索引 / 问答 / wiki 缓存），
供 `knowledge/deepwiki_open.py` 适配层调用。**保留 `probe()` 与
`build_deepwiki_client()` 的既有语义**，避免破坏 M0 的启动自检调用。

deepwiki-open（FastAPI，默认 :8001）已核实的端点：
- `GET  /health`                  健康检查
- `GET  /models/config`           provider/model 列表（用于 API 前缀探测）
- `POST /repo/prepare`            SSE 流：clone + 建索引，流结束即完成
- `GET  /repo/index/status`       查询仓库是否已索引
- `GET  /wiki_cache`              已生成 wiki 的 JSON 缓存
- `POST /chat/completions/stream` SSE 流：RAG 问答
- `POST /retrieval`               只检索：返回代码片段，不生成答案

注意：端点前缀可能带或不带 `/api`（不同版本）。本客户端做**一次性前缀探测并缓存**，
不会每次调用都探测。`references` 出处字段名尚未完全确定，`knowledge/` 层按尽力而为解析。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

from config import Config

logger = logging.getLogger("issue_master.deepwiki")

# `/repo/prepare` 首次 clone + 向量化可能很久，超时不得短于 10 分钟
_MIN_PREPARE_TIMEOUT = 600.0


def _split_repo(value: str) -> tuple[str, str]:
    """把 `https://github.com/owner/name` 或 `owner/name` 拆成 (owner, name)。"""
    raw = (value or "").strip().rstrip("/")
    if raw.startswith(("http://", "https://")):
        path = urlparse(raw).path.strip("/")
    else:
        path = raw
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        raise RuntimeError(f"无法解析仓库地址：{value}")
    return parts[-2], parts[-1]


def _response_snippet(resp: httpx.Response, limit: int = 300) -> str:
    """截取响应正文片段（单行化），用于错误信息，避免刷屏。"""
    try:
        text = resp.text or ""
    except Exception:  # noqa: BLE001 - 流式响应读取失败不应掩盖原始错误
        text = ""
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


class DeepWikiClient:
    """deepwiki-open HTTP 客户端（含一次性 API 前缀探测）。"""

    def __init__(self, base_url: str, timeout: float | None = None):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = float(timeout if timeout is not None else Config.kb_timeout)
        self._client = httpx.Client(timeout=self.timeout)
        # API 前缀探测结果缓存：None=未探测，''=无前缀，'/api'=带前缀
        self._prefix: str | None = None

    # ---------- 通用请求 ----------
    def _raise_http(self, resp: httpx.Response, action: str = "") -> None:
        """HTTP 错误统一抛出携带「状态码 + 响应片段」的异常，便于在飞书里回报原因。"""
        snippet = _response_snippet(resp)
        where = action or str(resp.request.url)
        raise RuntimeError(
            f"deepwiki-open 请求失败（{where}）：HTTP {resp.status_code}"
            + (f" —— {snippet}" if snippet else "")
        )

    def _api_prefix(self) -> str:
        """一次性探测 API 前缀（'' 或 '/api'）并缓存。

        先试不带 `/api`（GET /models/config），404 再试带 `/api`。
        探测不出（连接失败/均 404）时返回 ''，由真正的业务调用抛出带状态码的清晰错误。
        """
        if self._prefix is not None:
            return self._prefix
        for prefix in ("", "/api"):
            try:
                r = self._client.get(f"{self.base_url}{prefix}/models/config")
            except Exception as exc:  # noqa: BLE001 - 连接失败则无法判定前缀，交给业务调用报错
                logger.warning("探测 deepwiki-open API 前缀失败：%s", exc)
                break
            if r.status_code != 404:
                self._prefix = prefix
                logger.info("deepwiki-open API 前缀探测结果：'%s'", prefix)
                return prefix
        self._prefix = ""
        logger.info("deepwiki-open API 前缀探测未命中，按无前缀处理（''）")
        return ""

    # ---------- 探测 ----------
    def probe(self) -> str:
        """探测知识库服务连通性；失败抛异常，成功返回一句人类可读描述。

        优先 GET `/health`；若服务无该端点（404）则回退到根路径，保持 M0 的宽松判定。
        """
        r = self._client.get(self.base_url + "/health")
        if r.status_code == 404:
            r = self._client.get(self.base_url + "/")
        if r.status_code >= 400:
            self._raise_http(r, action="探测 /health")
        prefix = self._api_prefix()
        return f"{self.base_url} 可达（HTTP {r.status_code}，API 前缀 '{prefix or '/'}'）"

    # ---------- 索引状态 ----------
    def index_status(self, repo_url: str) -> dict:
        """查询仓库是否已在本地 FAISS 库中索引，返回原始 JSON。"""
        prefix = self._api_prefix()
        r = self._client.get(
            f"{self.base_url}{prefix}/repo/index/status", params={"repo_url": repo_url}
        )
        if r.status_code >= 400:
            self._raise_http(r, action="查询索引状态")
        data = r.json()
        return data if isinstance(data, dict) else {"raw": data}

    # ---------- 建立索引（SSE）----------
    def prepare_repo(
        self, repo_url: str, token: str | None = None, force: bool = False
    ) -> str:
        """POST `/repo/prepare`：流式读完 SSE，累计进度信息，返回最后一条进度/完成信息。

        超时按 `Config.kb_timeout`，且不得短于 `_MIN_PREPARE_TIMEOUT`（首次索引很慢）。
        `force=True` 时要求 deepwiki 忽略已落盘索引、重新 clone 并重建。
        """
        prefix = self._api_prefix()
        payload: dict[str, Any] = {"repo_url": repo_url, "type": "github"}
        if token:
            payload["token"] = token
        if force:
            payload["force"] = True
        timeout = max(self.timeout, _MIN_PREPARE_TIMEOUT)
        last = ""
        with self._client.stream(
            "POST",
            f"{self.base_url}{prefix}/repo/prepare",
            json=payload,
            timeout=timeout,
        ) as resp:
            if resp.status_code >= 400:
                resp.read()
                self._raise_http(resp, action="触发仓库索引")
            for item in self._iter_sse(resp):
                # deepwiki-open 建索引失败时会发 `event: error` + `data: {"error": ...}`；
                # 必须在此抛错，否则流正常结束会被上层误判为「索引成功」。
                if isinstance(item, dict):
                    err = item.get("error")
                    if isinstance(err, str) and err.strip():
                        raise RuntimeError(f"deepwiki-open 建索引失败：{err.strip()}")
                last = self._describe_sse_item(item)
                logger.info("deepwiki-open 索引进度（%s）：%s", repo_url, last)
        return last or "索引流已结束（未收到进度文本）"

    # ---------- 问答（SSE）----------
    def chat(
        self,
        repo_url: str,
        messages: list[dict],
        provider: str | None = None,
        model: str | None = None,
        language: str | None = None,
        file_path: str | None = None,
        token: str | None = None,
    ) -> Iterator[Any]:
        """POST `/chat/completions/stream`，逐行产出流内容（纯文本 token；兼容 JSON 负载）。"""
        prefix = self._api_prefix()
        payload: dict[str, Any] = {
            "repo_url": repo_url,
            "messages": messages,
            "type": "github",
        }
        if file_path:
            payload["filePath"] = file_path
        if token:
            payload["token"] = token
        if provider:
            payload["provider"] = provider
        if model:
            payload["model"] = model
        if language:
            payload["language"] = language
        with self._client.stream(
            "POST", f"{self.base_url}{prefix}/chat/completions/stream", json=payload
        ) as resp:
            if resp.status_code >= 400:
                resp.read()
                self._raise_http(resp, action="知识库问答")
            for item in self._iter_sse(resp, decode_json=False):
                # deepwiki-open 的问答流是纯文本 token（见 api/chat/_stream.py: yield text），
                # 因此不做 JSON 解析、也不按 dict 过滤：否则正文会被整体丢掉，问答恒为空。
                yield item

    # ---------- 只检索（不生成）----------
    def retrieve(
        self,
        repo_url: str,
        query: str,
        top_k: int = 8,
        provider: str | None = None,
        model: str | None = None,
        token: str | None = None,
    ) -> dict:
        """POST `/retrieval`：只返回检索片段，不触发答案生成。

        返回原始 JSON；字段归一由 `knowledge/deepwiki_open.py` 负责。
        """
        prefix = self._api_prefix()
        payload: dict[str, Any] = {
            "repo_url": repo_url,
            "type": "github",
            "query": query,
            "top_k": int(top_k),
        }
        if token:
            payload["token"] = token
        if provider:
            payload["provider"] = provider
        if model:
            payload["model"] = model
        r = self._client.post(f"{self.base_url}{prefix}/retrieval", json=payload)
        if r.status_code >= 400:
            self._raise_http(r, action="知识库检索")
        data = r.json()
        return data if isinstance(data, dict) else {"raw": data}

    # ---------- wiki 缓存 ----------
    def wiki_cache(self, repo_url: str) -> dict:
        """GET `/api/wiki_cache`：取该仓库已生成的 wiki JSON。

        deepwiki-open 的这个路由固定挂在 `/api` 前缀下，且按 owner/repo/repo_type/
        language 四个独立参数查询（不是 `repo_url`），因此不能复用 `_api_prefix()`。
        未生成过 wiki 时接口返回 200 + null，这里统一归一为空 dict。
        """
        owner, name = _split_repo(repo_url)
        r = self._client.get(
            f"{self.base_url}/api/wiki_cache",
            params={
                "owner": owner,
                "repo": name,
                "repo_type": "github",
                "language": Config.kb_language or "en",
            },
        )
        if r.status_code == 404:
            return {}
        if r.status_code >= 400:
            self._raise_http(r, action="读取 wiki 缓存")
        data = r.json()
        return data if isinstance(data, dict) else {}

    # ---------- SSE 解析 ----------
    @staticmethod
    def _iter_sse(resp: httpx.Response, decode_json: bool = True) -> Iterator[Any]:
        """逐行解析 SSE：`data:` 负载尝试 JSON 解析，失败则原样返回字符串。

        忽略空行、`[DONE]`、心跳与 `event:`/`id:`/`retry:` 控制行。
        `decode_json=False` 时一律返回原始文本、不做 JSON 解析——问答流用这个模式：
        deepwiki-open 的 `/chat/completions/stream` 发的是纯文本 token，若仍做 JSON
        解析，答案里形如 `{"mode": "standard"}` 的整行会被解析成 dict 而被丢掉。
        """
        for raw in resp.iter_lines():
            line = (raw or "").strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
            elif line.split(":", 1)[0] in ("event", "id", "retry"):
                # SSE 控制行：不参与内容解析
                continue
            else:
                payload = line
            if not payload or payload == "[DONE]":
                continue
            if not decode_json:
                yield payload
                continue
            try:
                yield json.loads(payload)
            except (ValueError, TypeError):
                yield payload

    @staticmethod
    def _describe_sse_item(item: Any) -> str:
        """把一条 SSE 负载转成可读的单行进度文本。"""
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            for key in ("message", "status", "detail", "stage", "text", "msg"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return json.dumps(item, ensure_ascii=False)[:300]
        return str(item)


def build_deepwiki_client() -> DeepWikiClient:
    return DeepWikiClient(Config.kb_base_url)
