"""Agent 层核心接口：请求 / 结果 dataclass 与后端协议。

约定（对齐参考项目）：除 `agents/` 之外的任何模块都不允许直接调用模型
（不 import 任何 LLM SDK、不发 `/chat/completions`），一律经由 `agents.registry`。

本阶段（M0）只定义结构，不含任何业务实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ---------------- 引用出处 ----------------

@dataclass
class Ref:
    """一条知识库/代码/提交的引用出处（issue 处理草稿必须带真实出处）。"""

    kind: str                    # "wiki" | "file" | "commit"
    path: str = ""
    url: str = ""
    snippet: str = ""


# ---------------- issue 巡检 ----------------

@dataclass
class IssueMeta:
    """一条 issue 的元信息（供巡检汇总使用）。

    `timeline_events` / `linked_prs` / `days_since_last_comment` 为 M2 巡检补充的事实字段：
    均来自 GitHub 客观返回（timeline 事件 / 关联 PR / 评论时间），**不由模型臆断**，
    供模型归纳「当前阶段 / 卡点 / 最近动向 / 建议下一步」时作为唯一依据。
    """

    repo: str
    number: int
    title: str
    author: str = ""
    html_url: str = ""
    created_at: str = ""
    updated_at: str = ""
    state: str = "open"
    labels: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    comments_count: int = 0
    last_comment_at: str = ""
    last_comment_author: str = ""
    body: str = ""
    # 最近 timeline 关键事件（人类可读短句，如「指派 alice」「关联 #600（PR）」）
    timeline_events: list[str] = field(default_factory=list)
    # 关联 PR：{number, url, state, merged, updated_at}；merged 未知时为 None
    linked_prs: list[dict] = field(default_factory=list)
    # 距最后一条评论的天数；无评论时为「距创建」的天数，-1 表示时间不可解析
    days_since_last_comment: int = -1


@dataclass
class IssueDigestRequest:
    """一次 issue 巡检汇总请求的全部输入。"""

    repos: list[str]
    days: int
    issues: list[IssueMeta] = field(default_factory=list)
    extra: str = ""


@dataclass
class IssueProgress:
    """单个 issue 的进展归纳：当前阶段 / 卡点 / 最近动向 / 建议下一步。"""

    repo: str
    number: int
    stage: str = ""
    blockers: str = ""
    recent: str = ""
    next_step: str = ""


@dataclass
class IssueDigestResult:
    """一次巡检汇总的结构化结果。"""

    overview: str = ""
    items: list[IssueProgress] = field(default_factory=list)


# ---------------- issue 处理 ----------------

@dataclass
class IssueReplyRequest:
    """一次 issue 处理请求的全部输入。"""

    repo: str
    number: int
    title: str = ""
    body: str = ""
    comments: list[dict] = field(default_factory=list)
    timeline: list[dict] = field(default_factory=list)
    # 发起人在飞书里给的额外提示词
    hint: str = ""
    # 知识库检索得到的上下文（由 knowledge/ 层提供）
    kb_context: str = ""
    extra: str = ""


@dataclass
class IssueDraft:
    """一份待人工确认的 issue 处理草稿。"""

    summary: str = ""
    root_cause_hypothesis: str = ""
    comment_md: str = ""
    # 强副作用动作（关闭/指派/改标签等），必须逐项勾选，不默认勾选
    proposed_actions: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    references: list[Ref] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "root_cause_hypothesis": self.root_cause_hypothesis,
            "comment_md": self.comment_md,
            "proposed_actions": self.proposed_actions,
            "confidence": self.confidence,
            "references": [r.__dict__ for r in self.references],
            "open_questions": self.open_questions,
        }


# ---------------- 自由问答（/kb ask）----------------

@dataclass
class AnswerRequest:
    """一次知识库自由问答请求。"""

    repo: str
    question: str
    extra: str = ""


@dataclass
class AnswerResult:
    """一次知识库自由问答的结果。"""

    text: str = ""
    references: list[Ref] = field(default_factory=list)


# ---------------- 后端协议 ----------------

@runtime_checkable
class AgentBackend(Protocol):
    """Agent 后端协议。第三方后端只需实现这五个方法。"""

    name: str

    def available(self) -> tuple[bool, str]:
        """（是否可用，原因）。不可用时 registry 会按降级链继续尝试。"""
        ...

    def probe(self) -> str:
        """连通性自检，返回一句人类可读的描述；失败请抛异常。"""
        ...

    def issue_digest(self, req: IssueDigestRequest) -> IssueDigestResult:
        """归纳一批 issue 的当前阶段 / 卡点 / 最近动向 / 建议下一步。"""
        ...

    def issue_reply(self, req: IssueReplyRequest) -> IssueDraft:
        """基于知识库与代码上下文产出 issue 处理草稿。"""
        ...

    def answer(self, req: AnswerRequest) -> AnswerResult:
        """知识库自由问答（/kb ask）。"""
        ...
