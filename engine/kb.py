"""知识库编排层：索引刷新（含 sha 比对跳过）+ 问答（缓存 + 多轮）+ 状态汇总。

只做「拉数据 → 组装请求 → 转交知识库 → 落库」；**不直接调用任何模型**。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time

from agents.base import AnswerResult, Ref
from config import Config
from knowledge.base import RepoKnowledgeBase
from store.db import Database

logger = logging.getLogger("issue_master.kb")


class KbEngine:
    """知识库编排：刷新 / 问答 / 状态。"""

    def __init__(self, db: Database, github, kb: RepoKnowledgeBase):
        self.db = db
        self.github = github
        self.kb = kb
        # 全局刷新锁（refresh 不可并发；非阻塞获取，已在跑则返回 busy）
        self._refresh_lock = threading.Lock()
        # 进程内多轮会话历史：(chat_id, repo) -> [{"role","content"}]
        self._history: dict[tuple[str, str], list[dict]] = {}
        self._history_lock = threading.Lock()

    # ---------------- 持有仓库 ----------------
    def repos(self) -> list[str]:
        """从 kb_config.repos 读取持有仓库列表。"""
        cfg = self.db.get_kb_config()
        return [r.strip() for r in (cfg["repos"] or "").split(",") if r.strip()]

    # ---------------- 刷新 ----------------
    def refresh(self, repos: list[str] | None = None, trigger: str = "manual") -> list[dict]:
        """刷新索引：sha 未变则跳过；变了则重建并失效缓存。返回逐仓库结果列表。

        `trigger` 仅用于日志与回报（manual/scheduled）。
        """
        if not self._refresh_lock.acquire(blocking=False):
            return [
                {
                    "repo": "*",
                    "status": "busy",
                    "sha": "",
                    "detail": "已有刷新任务在运行（全局锁），本次未执行",
                    "elapsed": 0.0,
                }
            ]
        try:
            targets = repos or self.repos()
            results: list[dict] = []
            logger.info("知识库刷新开始（trigger=%s，仓库=%s）", trigger, ", ".join(targets) or "（空）")
            for repo in targets:
                results.append(self._refresh_one(repo))
            return results
        finally:
            self._refresh_lock.release()

    def _refresh_one(self, repo: str) -> dict:
        start = time.time()

        def _done(status: str, sha: str, detail: str) -> dict:
            return {
                "repo": repo,
                "status": status,
                "sha": sha,
                "detail": detail,
                "elapsed": round(time.time() - start, 1),
            }

        # 1. 取仓库最新 sha（失败 → failed，继续下一个仓库）
        try:
            sha = self.github.get_latest_commit_sha(repo)
        except Exception as exc:  # noqa: BLE001
            msg = f"取最新 commit sha 失败：{exc}"
            self.db.mark_kb_index_check(repo, "failed", last_error=msg)
            return _done("failed", "", msg)

        # 2. 与已索引 sha 比对，相同则跳过（M1 验收点）
        row = self.db.get_kb_index(repo)
        prev = (row["indexed_sha"] if row and row["indexed_sha"] else "") or ""
        if prev and prev == sha:
            self.db.mark_kb_index_check(repo, "fresh")
            return _done("skipped", sha, f"sha 未变（{sha[:8]}），跳过重建")

        # 3. sha 不同 → 强制重建索引
        try:
            status = self.kb.ensure_indexed(repo, force=True)
        except Exception as exc:  # noqa: BLE001
            msg = f"重建索引失败：{exc}"
            self.db.mark_kb_index_check(repo, "failed", last_error=msg)
            return _done("failed", sha, msg)
        if status.status != "indexed":
            msg = f"重建后索引状态仍为 {status.status}：{status.detail}"
            self.db.mark_kb_index_check(repo, "failed", last_error=msg)
            return _done("failed", sha, msg)

        # 4. 落库 + 失效该仓库问答缓存
        self.db.upsert_kb_index(repo, "fresh", sha)
        self.db.mark_kb_index_check(repo, "fresh", indexed_sha=sha)
        removed = self.db.invalidate_kb_cache(repo)
        detail = status.detail or "已重建索引"
        if removed:
            detail += f"；失效缓存 {removed} 条"
        return _done("fresh", sha, detail)

    # ---------------- 问答 ----------------
    def ask(self, chat_id: str, repo: str, question: str) -> AnswerResult:
        """多轮问答：命中缓存则直接返回；否则带历史调用并写缓存、记历史。"""
        repo = (repo or Config.github_default_repo).strip()
        question = (question or "").strip()

        row = self.db.get_kb_index(repo)
        index_state = (row["status"] if row else "") or ""
        indexed_sha = ((row["indexed_sha"] if row else "") or "")

        question_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()
        cache_key = hashlib.sha256(
            f"{repo}\0{question}\0{indexed_sha}".encode("utf-8")
        ).hexdigest()

        cached = self.db.get_kb_cache(cache_key)
        if cached:
            answer = self._answer_from_json(cached["answer_json"])
        else:
            messages = self._history_for(chat_id, repo) + [
                {"role": "user", "content": question}
            ]
            answer = self.kb.ask(repo, messages)
            # 仅缓存非空回答：知识库报错 / 瞬时无返回时 answer.text 为空，
            # 若把空回答写进缓存，会被同一条提问**永久命中回放**，表现为
            # 「知识库未返回内容」自我固化（此前客户端解析修复前写入的空行即此因）。
            if answer.text.strip():
                self.db.put_kb_cache(
                    cache_key,
                    repo,
                    question_hash,
                    json.dumps(
                        {
                            "text": answer.text,
                            "references": [r.__dict__ for r in answer.references],
                        },
                        ensure_ascii=False,
                    ),
                )
            self._append_history(chat_id, repo, question, answer.text)

        # 索引未就绪时不静默失败：正常尝试问答，但明确告警
        if index_state != "fresh":
            warn = f"⚠️ 该仓库索引状态为 {index_state or '无记录'}（可能不是最新）\n"
            return AnswerResult(text=warn + answer.text, references=answer.references)
        return answer

    def search(self, chat_id: str, repo: str, question: str, top_k: int = 8) -> dict:
        """只检索：返回片段 + 索引状态；不做问答缓存（检索依赖问题原文）。

        索引不是 fresh 时不阻断，但在返回值里给出 `warning`，由调用方如实展示。
        """
        repo = (repo or Config.github_default_repo).strip()
        question = (question or "").strip()
        if not question:
            raise ValueError("question 不能为空")
        row = self.db.get_kb_index(repo)
        index_state = (row["status"] if row else "") or ""
        indexed_sha = ((row["indexed_sha"] if row else "") or "")
        chunks = self.kb.retrieve(repo, question, top_k=top_k)
        warning = (
            ""
            if index_state == "fresh"
            else f"该仓库索引状态为 {index_state or '无记录'}（可能不是最新，请先刷新索引）"
        )
        return {
            "repo": repo,
            "chunks": chunks,
            "index": {"status": index_state or "unknown", "sha": indexed_sha},
            "warning": warning,
        }

    def wiki(self, repo: str, topic: str) -> str:
        """读取 wiki 页面正文（找不到返回空串，不编造）。"""
        return self.kb.get_wiki((repo or Config.github_default_repo).strip(), (topic or "").strip())

    def _history_for(self, chat_id: str, repo: str) -> list[dict]:
        with self._history_lock:
            return [dict(m) for m in self._history.get((chat_id, repo), [])]

    def _append_history(self, chat_id: str, repo: str, question: str, answer_text: str) -> None:
        limit = max(1, Config.kb_history_turns) * 2  # 1 轮 = user + assistant
        with self._history_lock:
            hist = self._history.setdefault((chat_id, repo), [])
            hist.append({"role": "user", "content": question})
            hist.append({"role": "assistant", "content": answer_text})
            if len(hist) > limit:
                del hist[: len(hist) - limit]

    @staticmethod
    def _answer_from_json(blob: str) -> AnswerResult:
        try:
            data = json.loads(blob)
        except (ValueError, TypeError):
            return AnswerResult(text="")
        refs = [
            Ref(
                kind=str(r.get("kind", "")),
                path=str(r.get("path", "")),
                url=str(r.get("url", "")),
                snippet=str(r.get("snippet", "")),
            )
            for r in (data.get("references") or [])
            if isinstance(r, dict)
        ]
        return AnswerResult(text=str(data.get("text", "")), references=refs)

    # ---------------- 状态 ----------------
    def status(self) -> list[dict]:
        """给 /kb status 用：每个持有仓库的索引状态。"""
        out: list[dict] = []
        for repo in self.repos():
            row = self.db.get_kb_index(repo)
            out.append(
                {
                    "repo": repo,
                    "status": ((row["status"] if row else "") or "unknown"),
                    "indexed_sha": ((row["indexed_sha"] if row else "") or ""),
                    "indexed_at": ((row["indexed_at"] if row else "") or ""),
                    "last_check_at": ((row["last_check_at"] if row else "") or ""),
                    "last_error": ((row["last_error"] if row else "") or ""),
                }
            )
        return out
