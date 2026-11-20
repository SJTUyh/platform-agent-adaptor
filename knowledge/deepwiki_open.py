"""自托管 deepwiki-open 知识库后端（默认实现）。

职责：把 `adapters/deepwiki_client.DeepWikiClient` 的原始 HTTP 调用包装成
`knowledge.base.RepoKnowledgeBase` 协议；`repo`（"owner/name"）→ `repo_url`
映射规则统一为 `https://github.com/{repo}`。

注意：deepwiki-open 返回的 `references`/`sources` 字段名尚未完全确定，
解析按**尽力而为**处理——解析不到就返回空列表，绝不编造出处。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from adapters.deepwiki_client import DeepWikiClient
from config import Config
from knowledge.base import AnswerResult, IndexStatus, Ref

logger = logging.getLogger("agent_adaptor.kb.deepwiki")

# 索引状态原文 → 本项目归一状态
_INDEXED_WORDS = ("indexed", "ready", "success", "completed", "done", "ok", "fresh")
_INDEXING_WORDS = ("indexing", "processing", "preparing", "cloning", "building", "running")
_FAILED_WORDS = ("failed", "error", "exception")

# 出处可能的字段名（best-effort，字段名待实测确认）
_REF_LIST_KEYS = ("references", "sources", "citations", "docs", "context")
_TEXT_KEYS = ("content", "text", "response", "answer", "delta", "chunk")


def repo_to_url(repo: str) -> str:
    """`owner/name` → `https://github.com/owner/name`（已是 URL 则原样返回）。"""
    r = (repo or "").strip().rstrip("/")
    if r.startswith("http://") or r.startswith("https://"):
        return r
    return f"https://github.com/{r}"


def _as_int(value: Any) -> int:
    """把检索片段里的行号安全转成 int（缺失/非法时归 0）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    """把检索分数安全转成 float（缺失/非法时归 0.0）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class DeepWikiOpenKB:
    """deepwiki-open 后端。"""

    name = "deepwiki_open"

    def __init__(self, client: DeepWikiClient | None = None):
        self._client = client or DeepWikiClient(Config.kb_base_url)

    # ---------- 可用性 / 探测 ----------
    def available(self) -> tuple[bool, str]:
        if not Config.kb_base_url:
            return False, "KB_BASE_URL 未配置（知识库地址为空）"
        return True, ""

    def probe(self) -> str:
        return self._client.probe()

    # ---------- 索引状态 ----------
    def index_status(self, repo: str) -> IndexStatus:
        data = self._client.index_status(repo_to_url(repo))
        return IndexStatus(
            repo=repo,
            status=self._normalize_status(data),
            indexed_sha=self._extract_sha(data),
            detail=json.dumps(data, ensure_ascii=False)[:300],
            raw=data,
        )

    def ensure_indexed(
        self, repo: str, force: bool = False, token: str = ""
    ) -> IndexStatus:
        if not force:
            st = self.index_status(repo)
            if st.status == "indexed":
                return st
        repo_url = repo_to_url(repo)
        # prepare_repo 是 SSE 流，流结束即完成；异常会向上抛出，由编排层落 failed
        # force 透传给 deepwiki：要求其忽略已落盘索引、重新 clone 并重建
        detail = self._client.prepare_repo(repo_url, token=token or None, force=force)
        after = IndexStatus(repo=repo, status="indexed", detail=detail)
        try:
            after = self.index_status(repo)
            after.detail = detail or after.detail
        except Exception as exc:  # noqa: BLE001 - 建索引已成功，状态回读失败仅告警
            logger.warning("索引完成后回读状态失败（%s）：%s", repo, exc)
        # 流已正常结束，视为索引完成；即便状态回读为 unknown 也不谎报失败
        return IndexStatus(
            repo=repo,
            status="indexed",
            indexed_sha=after.indexed_sha,
            detail=detail or after.detail,
            raw=after.raw,
        )

    # ---------- 问答 ----------
    def ask(self, repo: str, messages: list[dict], token: str = "") -> AnswerResult:
        chunks = self._client.chat(
            repo_to_url(repo),
            messages,
            provider=Config.kb_provider or None,
            model=Config.kb_model or None,
            language=Config.kb_language or None,
            token=token or None,
        )
        parts: list[str] = []
        refs: list[Ref] = []
        seen: set[tuple[str, str]] = set()
        for chunk in chunks:
            if isinstance(chunk, str):
                # deepwiki-open 的问答流直接产出正文 token；原样累积（不 strip，避免吃掉词间空格）
                if chunk:
                    parts.append(chunk)
                continue
            err = chunk.get("error")
            if isinstance(err, str) and err.strip():
                raise RuntimeError(f"deepwiki-open 问答返回错误：{err.strip()}")
            text = self._extract_text(chunk)
            if text:
                parts.append(text)
            for ref in self._extract_refs(chunk):
                key = (ref.url, ref.path)
                if key not in seen:
                    seen.add(key)
                    refs.append(ref)
        # 问答流按行返回，用 "\n" 拼回以保留 markdown 的段落/列表/代码块结构
        return AnswerResult(text="\n".join(parts).strip(), references=refs)

    # ---------- 只检索 ----------
    def retrieve(
        self, repo: str, query: str, top_k: int = 8, token: str = ""
    ) -> list[dict]:
        """调用 `/retrieval`，把片段归一为 {path,startLine,endLine,title,text,score}。

        路径缺失的片段直接丢弃（无法核对出处，不能交给 Agent 当依据）。
        """
        data = self._client.retrieve(
            repo_to_url(repo),
            query,
            top_k=top_k,
            provider=Config.kb_provider or None,
            model=Config.kb_model or None,
            token=token or None,
        )
        chunks: list[dict] = []
        raw_chunks = data.get("chunks")
        if not isinstance(raw_chunks, list):
            return chunks
        for item in raw_chunks:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            text = str(item.get("text") or "")
            if not text.strip():
                continue
            chunks.append(
                {
                    "path": path,
                    "startLine": _as_int(item.get("startLine")),
                    "endLine": _as_int(item.get("endLine")),
                    "title": str(item.get("title") or path),
                    "text": text[:1200],
                    "score": _as_float(item.get("score")),
                }
            )
        return chunks

    # ---------- wiki ----------
    def get_wiki(self, repo: str, topic: str) -> str:
        data = self._client.wiki_cache(repo_to_url(repo))
        return self._find_wiki_page(data, topic)

    # ---------- 内部解析（best-effort，字段名待实测确认）----------
    @staticmethod
    def _normalize_status(data: dict) -> str:
        indexed = data.get("indexed")
        raw = str(data.get("status", "") or "").lower().strip()
        if isinstance(indexed, bool) and indexed:
            return "indexed"
        if raw in _INDEXED_WORDS:
            return "indexed"
        if raw in _INDEXING_WORDS:
            return "indexing"
        if raw in _FAILED_WORDS:
            return "failed"
        if indexed is False:
            return "not_indexed"
        return raw or "unknown"

    @staticmethod
    def _extract_sha(data: dict) -> str:
        for key in ("indexed_sha", "sha", "commit_sha", "last_commit_sha", "commit", "last_commit"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = value.get("sha") or value.get("id")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
        return ""

    @staticmethod
    def _extract_text(chunk: dict) -> str:
        for key in _TEXT_KEYS:
            value = chunk.get(key)
            if isinstance(value, str) and value:
                return value
        # 退化：`choices[0].delta.content`（OpenAI 风格）
        choices = chunk.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            delta = first.get("delta") or first.get("message") or {}
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str):
                    return content
        return ""

    @classmethod
    def _extract_refs(cls, chunk: dict) -> list[Ref]:
        out: list[Ref] = []
        for key in _REF_LIST_KEYS:
            value = chunk.get(key)
            if isinstance(value, list):
                for item in value:
                    ref = cls._to_ref(item)
                    if ref:
                        out.append(ref)
        return out

    @staticmethod
    def _to_ref(item: Any) -> Ref | None:
        if isinstance(item, str):
            text = item.strip()
            return Ref(kind="wiki", path=text) if text else None
        if not isinstance(item, dict):
            return None
        path = str(item.get("path") or item.get("file") or item.get("filePath") or "")
        url = str(item.get("url") or item.get("link") or item.get("html_url") or "")
        title = str(item.get("title") or item.get("name") or "")
        snippet = str(item.get("snippet") or item.get("content") or item.get("text") or "")
        if not (path or url or title):
            return None
        kind = "file" if path and "." in path.rsplit("/", 1)[-1] else "wiki"
        return Ref(kind=kind, path=path or title, url=url, snippet=snippet[:500])

    @classmethod
    def _find_wiki_page(cls, data: dict, topic: str) -> str:
        """在 wiki JSON 里按页面标题/slug 匹配 topic，返回该页 markdown 正文。"""
        topic_l = (topic or "").strip().lower()
        pages = cls._collect_pages(data)
        generated = data.get("generated_pages")
        if not isinstance(generated, dict):
            generated = {}
        for page in pages:
            title = str(page.get("title", "") or "")
            slug = str(page.get("slug", "") or page.get("id", "") or "")
            pid = str(page.get("id", "") or slug)
            hay = f"{title} {slug} {pid}".lower()
            if topic_l and topic_l not in hay:
                continue
            content = (
                generated.get(pid)
                or page.get("content")
                or page.get("markdown")
                or page.get("body")
                or page.get("text")
            )
            if isinstance(content, str) and content.strip():
                return content
        # 退化：topic 直接命中 generated_pages 的键
        if topic_l:
            for pid, content in generated.items():
                if topic_l in str(pid).lower() and isinstance(content, str) and content.strip():
                    return content
        return ""

    @staticmethod
    def _collect_pages(data: dict) -> list[dict]:
        """从 wiki JSON 里尽力收集页面列表（结构因版本而异）。"""
        pages: list[dict] = []
        structure = data.get("wiki_structure")
        if isinstance(structure, dict) and isinstance(structure.get("pages"), list):
            pages = [p for p in structure["pages"] if isinstance(p, dict)]
        if not pages and isinstance(data.get("pages"), list):
            pages = [p for p in data["pages"] if isinstance(p, dict)]
        return pages
