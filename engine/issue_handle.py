"""issue 处理编排层：指令 → 拉事实 + 知识库检索 → Agent 产出草稿 → 人工确认 → 机器人身份回写。

只做「拉数据 → 组装请求 → 转交 Agent / 知识库 → 落库 → 回写」，**不直接调用任何模型**
（模型调用一律走 `agents`）。

契合 PLAN §6.6 的 `build_context()` / `publish()` 名义接口；同一 issue 串行处理（issue 级锁），
避免多人并发指令互相覆盖。强副作用动作（加/移标签、指派、关闭）必须由真人勾选后才执行。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field

from agents.base import IssueDraft, IssueReplyRequest, Ref
from config import Config

logger = logging.getLogger("issue_master.handle")

# 强副作用动作的中文标签（用于卡片与回执展示）
ACTION_LABELS = {
    "add_label": "添加标签",
    "remove_label": "移除标签",
    "assign": "指派",
    "close": "关闭 issue",
    "reopen": "重新打开 issue",
}
# issue 处理的知识库检索使用的内部会话标识（与飞书群 chat_id 区分开）
_KB_CHAT_ID = "agent"

# 外部（用户 Agent）发布允许的动作与长度上限
EXTERNAL_ACTION_KINDS = ("add_label", "remove_label", "assign", "close", "reopen")
MAX_EXTERNAL_COMMENT = 20000
MAX_EXTERNAL_ACTIONS = 10


@dataclass
class DraftAction:
    """草稿里一条待勾选的强副作用动作。"""

    index: int
    kind: str
    value: str = ""
    reason: str = ""

    @property
    def label(self) -> str:
        base = ACTION_LABELS.get(self.kind, self.kind or "动作")
        return f"{base}：{self.value}" if self.value else base


@dataclass
class DraftView:
    """渲染结果：可交给 `adapters.feishu_bot.card_issue_draft` 转成确认卡片。"""

    draft_id: int
    task_id: int
    repo: str
    number: int
    title: str
    url: str
    requested_by: str = ""
    summary: str = ""
    root_cause_hypothesis: str = ""
    comment_md: str = ""
    confidence: float = 0.0
    references: list[Ref] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    actions: list[DraftAction] = field(default_factory=list)
    kb_note: str = ""


@dataclass
class PublishResult:
    """一次发布的结果：评论回链 + 已执行动作 + 失败动作。"""

    draft_id: int
    comment_url: str = ""
    applied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # 幂等键命中时的回放标记（未执行任何新动作）
    replayed: bool = False


class IssueHandleEngine:
    """issue 处理编排：草稿生成 / 发布 / 丢弃 / 重生成。"""

    def __init__(self, db, github, backend, kb_engine=None):
        self.db = db
        self.github = github
        self.backend = backend
        self.kb_engine = kb_engine
        # issue 级锁：同一 (repo, number) 串行处理
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ---------------- 任务 ----------------
    def start(self, repo: str, number: int, requester_open_id: str, hint: str = "") -> int:
        """建一条处理指令（status=open），返回 task_id。"""
        return self.db.create_issue_task(repo, number, requester_open_id, hint)

    def _lock_for(self, repo: str, number: int) -> threading.Lock:
        key = f"{repo}#{int(number)}"
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
        return lock

    # ---------------- 草稿生成 ----------------
    def generate(self, task_id: int) -> DraftView:
        """执行一次草稿生成：拉事实 + 知识库检索 + Agent 产出 → 落库 → 返回 DraftView。"""
        task = self.db.get_issue_task(task_id)
        if task is None:
            raise RuntimeError(f"处理任务不存在：#{task_id}")
        repo = task["repo"]
        number = int(task["issue_number"])
        hint = task["hint"] or ""
        with self._lock_for(repo, number):
            issue, comments, timeline, kb_context, kb_note = self.build_context(repo, number, hint)
            req = IssueReplyRequest(
                repo=repo,
                number=number,
                title=issue.get("title") or "",
                body=issue.get("body") or "",
                comments=comments,
                timeline=timeline,
                hint=hint,
                kb_context=kb_context,
            )
            draft = self.backend.issue_reply(req)
            draft_id = self.db.upsert_issue_draft(
                task_id, draft.comment_md, json.dumps(draft.proposed_actions, ensure_ascii=False)
            )
            self.db.update_issue_task_status(task_id, "consumed")
            return self._to_view(draft, task, issue, draft_id, kb_note)

    def analyze(self, repo: str, number: int, hint: str = "") -> DraftView:
        """只读详细分析：拉事实 + 知识库检索 → Agent 产出分析（不落库、不外发、无副作用）。

        供巡检卡片「查看详情」按钮按需调用：与草稿生成共用同一套事实/知识库/模型链路，
        但**不写入 issue_tasks / issue_drafts**，也不带任何发布动作。
        """
        with self._lock_for(repo, number):
            issue, comments, timeline, kb_context, kb_note = self.build_context(repo, number, hint)
            req = IssueReplyRequest(
                repo=repo,
                number=number,
                title=issue.get("title") or "",
                body=issue.get("body") or "",
                comments=comments,
                timeline=timeline,
                hint=hint,
                kb_context=kb_context,
            )
            draft = self.backend.issue_reply(req)
            task = {"id": 0, "repo": repo, "issue_number": number, "requester_open_id": ""}
            return self._to_view(draft, task, issue, 0, kb_note)

    def build_context(
        self, repo: str, number: int, hint: str
    ) -> tuple[dict, list[dict], list[dict], str, str]:
        """拉取 issue 事实 + 知识库检索上下文。

        评论 / timeline 拉取失败按「未知」处理（不臆断为「无动向」）；
        知识库不可用不阻断草稿生成，仅在卡片上如实注明。
        """
        issue = self.github.get_issue(repo, number)
        try:
            comments = self.github.get_issue_comments(repo, number)
        except Exception as exc:  # noqa: BLE001 - 评论拉取失败不阻断草稿生成
            logger.warning("拉取 %s#%s 评论失败：%s", repo, number, exc)
            comments = []
        try:
            timeline = self.github.get_issue_timeline(repo, number)
        except Exception as exc:  # noqa: BLE001 - timeline 拉取失败不阻断草稿生成
            logger.warning("拉取 %s#%s timeline 失败：%s", repo, number, exc)
            timeline = []
        kb_context, kb_note = self._kb_context(repo, issue, hint)
        return issue, comments, timeline, kb_context, kb_note

    def _kb_context(self, repo: str, issue: dict, hint: str) -> tuple[str, str]:
        """知识库检索（best-effort）：失败/未接入时返回空上下文 + 提示语，不阻断。"""
        if self.kb_engine is None:
            return "", "未接入知识库（未做检索）"
        title = (issue.get("title") or "").strip()
        question = (hint or "").strip() or f"关于 issue「{title}」的相关实现与设计说明"
        if title and hint.strip() and title not in hint:
            question = f"{hint.strip()}（issue 标题：{title}）"
        try:
            answer = self.kb_engine.ask(_KB_CHAT_ID, repo, question)
        except Exception as exc:  # noqa: BLE001 - 知识库不可用不阻断草稿生成
            logger.warning("issue 处理的知识库检索失败（%s）：%s", repo, exc)
            return "", f"知识库检索失败：{exc}"
        lines: list[str] = []
        if answer.text.strip():
            lines.append(answer.text.strip())
        if answer.references:
            lines.append("出处：")
            for ref in answer.references:
                lines.append(f"- [{ref.kind or 'ref'}] {ref.path or ref.url}")
        note = "" if answer.text.strip() else "知识库未返回内容"
        return "\n".join(lines), note

    @staticmethod
    def _to_view(draft: IssueDraft, task, issue: dict, draft_id: int, kb_note: str) -> DraftView:
        actions = [
            DraftAction(
                index=i,
                kind=str(a.get("kind") or ""),
                value=str(a.get("value") or ""),
                reason=str(a.get("reason") or ""),
            )
            for i, a in enumerate(draft.proposed_actions or [])
            if isinstance(a, dict)
        ]
        return DraftView(
            draft_id=draft_id,
            task_id=int(task["id"]),
            repo=task["repo"],
            number=int(task["issue_number"]),
            title=issue.get("title") or "",
            url=issue.get("html_url") or "",
            requested_by=task["requester_open_id"] or "",
            summary=draft.summary,
            root_cause_hypothesis=draft.root_cause_hypothesis,
            comment_md=draft.comment_md,
            confidence=float(draft.confidence or 0.0),
            references=list(draft.references),
            open_questions=list(draft.open_questions),
            actions=actions,
            kb_note=kb_note,
        )

    # ---------------- 发布 / 丢弃 / 重生成 ----------------
    def publish(
        self, draft_id: int, operator_open_id: str, selected: dict | None = None
    ) -> PublishResult:
        """人工确认后由机器人身份回写：默认发表评论，勾选的动作逐项执行。"""
        selected = selected or {}
        row = self.db.get_issue_draft(draft_id)
        if row is None:
            raise RuntimeError(f"处理草稿不存在：#{draft_id}")
        task = self.db.get_issue_task(row["task_id"])
        if task is None:
            raise RuntimeError("草稿对应的处理任务不存在")
        if (row["status"] or "") != "pending_confirm":
            raise RuntimeError(f"该草稿当前状态为 {row['status']}，不可重复发布")
        if not self.can_operate(task["requester_open_id"] or "", operator_open_id):
            raise PermissionError("只有指令发起人或管理员可以确认发布")

        repo = task["repo"]
        number = int(task["issue_number"])
        with self._lock_for(repo, number):
            result = PublishResult(draft_id=draft_id)
            posted = self.github.post_comment(repo, number, row["comment_md"])
            result.comment_url = posted.get("html_url") or ""
            result.applied.append("发表评论")

            try:
                actions = json.loads(row["actions"] or "[]")
            except (ValueError, TypeError):
                actions = []
            for i, act in enumerate(actions):
                if not selected.get(f"act_{i}") or not isinstance(act, dict):
                    continue
                try:
                    note = self._apply_action(repo, number, act)
                except Exception as exc:  # noqa: BLE001 - 单个动作失败不影响其余
                    kind = ACTION_LABELS.get(str(act.get("kind") or ""), act.get("kind") or "动作")
                    result.errors.append(f"{kind} 执行失败：{exc}")
                    logger.error("issue 动作执行失败（%s#%s）：%s", repo, number, exc)
                    continue
                if note:
                    result.applied.append(note)

            self.db.update_issue_draft(draft_id, status="published", comment_url=result.comment_url)
            self.db.update_issue_task_status(task["id"], "closed")
            return result

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
        """外部 Agent 草稿经人工确认后的发布：可选只执行勾选动作。

        默认仍由机器人在 issue 下发评论；平台已用用户本人身份发评论时传
        `post_comment=False`，避免重复评论，仅执行强副作用动作。
        通过 `idempotency_key` 保证网络重试不会重复执行。
        """
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

    def regenerate(self, draft_id: int, operator_open_id: str) -> DraftView:
        """按原任务的提示词重新生成草稿（替换同一条在途草稿）。"""
        row = self.db.get_issue_draft(draft_id)
        if row is None:
            raise RuntimeError(f"处理草稿不存在：#{draft_id}")
        task = self.db.get_issue_task(row["task_id"])
        if task is None:
            raise RuntimeError("草稿对应的处理任务不存在")
        if not self.can_operate(task["requester_open_id"] or "", operator_open_id):
            raise PermissionError("只有指令发起人或管理员可以重新生成草稿")
        return self.generate(int(row["task_id"]))

    def discard(self, draft_id: int, operator_open_id: str) -> None:
        """丢弃草稿（不外发）。"""
        row = self.db.get_issue_draft(draft_id)
        if row is None:
            raise RuntimeError(f"处理草稿不存在：#{draft_id}")
        task = self.db.get_issue_task(row["task_id"])
        if task is not None and not self.can_operate(
            task["requester_open_id"] or "", operator_open_id
        ):
            raise PermissionError("只有指令发起人或管理员可以丢弃草稿")
        if (row["status"] or "") != "pending_confirm":
            raise RuntimeError(f"该草稿当前状态为 {row['status']}，不可丢弃")
        self.db.update_issue_draft(draft_id, status="rejected")
        if task is not None:
            self.db.update_issue_task_status(task["id"], "closed")

    # ---------------- 权限 ----------------
    @staticmethod
    def can_operate(requester_open_id: str, operator_open_id: str) -> bool:
        """仅发起人本人可操作（适配器不包含飞书管理员体系）。"""
        return bool(requester_open_id) and requester_open_id == operator_open_id
