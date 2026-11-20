"""外部 Agent 草稿发布：按人工确认结果发表评论并执行强副作用动作。

平台已用成员本人身份发布评论时传 `post_comment=False`，本模块只执行动作；
通过 `idempotency_key` 保证网络重试不会重复执行。本模块不调用任何模型。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field

logger = logging.getLogger("agent_adaptor.publish")

# 强副作用动作的中文标签（用于回执展示）
ACTION_LABELS = {
    "add_label": "添加标签",
    "remove_label": "移除标签",
    "assign": "指派",
    "close": "关闭 issue",
    "reopen": "重新打开 issue",
}

# 外部（用户 Agent）发布允许的动作与长度上限
EXTERNAL_ACTION_KINDS = ("add_label", "remove_label", "assign", "close", "reopen")
MAX_EXTERNAL_COMMENT = 20000
MAX_EXTERNAL_ACTIONS = 10


@dataclass
class PublishResult:
    """一次发布的结果：评论回链 + 已执行动作 + 失败动作。"""

    draft_id: int
    comment_url: str = ""
    applied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # 幂等键命中时的回放标记（未执行任何新动作）
    replayed: bool = False


class ExternalPublishEngine:
    """外部草稿发布：同一 (repo, number) 串行执行，动作失败不影响其余。"""

    def __init__(self, db, github):
        self.db = db
        self.github = github
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, repo: str, number: int) -> threading.Lock:
        key = f"{repo}#{int(number)}"
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
        return lock

    def publish_external(
        self,
        repo: str,
        number: int,
        comment_md: str,
        actions: list | None = None,
        requester: str = "",
        idempotency_key: str = "",
        post_comment: bool = True,
    ) -> PublishResult:
        """外部 Agent 草稿经人工确认后的发布：可选只执行勾选动作。"""
        repo = (repo or "").strip()
        number = int(number)
        comment = (comment_md or "").strip()
        if post_comment and not 1 <= len(comment) <= MAX_EXTERNAL_COMMENT:
            raise ValueError(f"comment_md 长度必须在 1~{MAX_EXTERNAL_COMMENT} 字符之间")
        normalized = self._normalize_external_actions(actions)
        key = (idempotency_key or "").strip()
        requester = (requester or "agent").strip()[:120] or "agent"

        if key:
            existing = self.db.get_external_publish(key)
            if existing is not None:
                return PublishResult(
                    draft_id=0,
                    comment_url=existing["comment_url"] or "",
                    applied=self._load_json_list(existing["applied_json"]),
                    errors=self._load_json_list(existing["errors_json"]),
                    replayed=True,
                )

        with self._lock_for(repo, number):
            result = PublishResult(draft_id=0)
            if post_comment:
                posted = self.github.post_comment(repo, number, comment)
                result.comment_url = posted.get("html_url") or ""
                result.applied.append("发表评论")
            for act in normalized:
                try:
                    note = self._apply_action(repo, number, act)
                except Exception as exc:  # noqa: BLE001 - 单个动作失败不影响其余
                    kind = ACTION_LABELS.get(act.get("kind") or "", act.get("kind") or "动作")
                    result.errors.append(f"{kind} 执行失败：{exc}")
                    logger.error("外部发布动作执行失败（%s#%s）：%s", repo, number, exc)
                    continue
                if note:
                    result.applied.append(note)
            if key:
                self.db.record_external_publish(
                    key,
                    repo,
                    number,
                    result.comment_url,
                    json.dumps(result.applied, ensure_ascii=False),
                    json.dumps(result.errors, ensure_ascii=False),
                    requester,
                )
            self.db.write_audit(
                requester,
                "issue.publish.external",
                target=f"{repo}#{number}",
                result=json.dumps(
                    {"applied": result.applied, "errors": result.errors}, ensure_ascii=False
                )[:2000],
            )
            return result

    @staticmethod
    def _normalize_external_actions(actions: list | None) -> list[dict]:
        """校验外部发布动作：只允许白名单动作，字段长度受限，非法即拒绝（不静默丢弃）。"""
        if actions is None:
            return []
        if not isinstance(actions, list):
            raise ValueError("actions 必须是数组")
        if len(actions) > MAX_EXTERNAL_ACTIONS:
            raise ValueError(f"actions 最多 {MAX_EXTERNAL_ACTIONS} 项")
        out: list[dict] = []
        for index, act in enumerate(actions, 1):
            if not isinstance(act, dict):
                raise ValueError(f"第 {index} 项动作必须是对象")
            kind = str(act.get("kind") or "").strip()
            if kind not in EXTERNAL_ACTION_KINDS:
                raise ValueError(f"第 {index} 项动作 kind 不支持：{kind}")
            value = str(act.get("value") or "").strip()
            reason = str(act.get("reason") or "").strip()
            if len(value) > 100:
                raise ValueError(f"第 {index} 项动作 value 不能超过 100 字符")
            if len(reason) > 500:
                raise ValueError(f"第 {index} 项动作 reason 不能超过 500 字符")
            if kind in ("close", "reopen"):
                value = ""
            elif not value:
                raise ValueError(f"第 {index} 项动作 {kind} 缺少 value")
            out.append({"kind": kind, "value": value, "reason": reason})
        return out

    @staticmethod
    def _load_json_list(blob: str | None) -> list[str]:
        try:
            data = json.loads(blob or "[]")
        except (ValueError, TypeError):
            return []
        return [str(item) for item in data] if isinstance(data, list) else []

    def _apply_action(self, repo: str, number: int, act: dict) -> str:
        """执行一条勾选通过的强副作用动作，返回人类可读描述。"""
        kind = str(act.get("kind") or "").strip()
        value = str(act.get("value") or "").strip()
        if kind == "add_label" and value:
            self.github.add_labels(repo, number, [value])
            return f"添加标签：{value}"
        if kind == "remove_label" and value:
            self.github.remove_label(repo, number, value)
            return f"移除标签：{value}"
        if kind == "assign" and value:
            self.github.assign(repo, number, [value])
            return f"指派：{value}"
        if kind == "close":
            self.github.close_issue(repo, number)
            return "关闭 issue"
        if kind == "reopen":
            self.github.reopen_issue(repo, number)
            return "重新打开 issue"
        return ""
