"""离线兜底后端：不依赖任何模型 SDK，用于自检与最后一层兜底。

它**不会**真的检视 issue 或代码，只会把请求回显出来并明确告警，
避免「后端不可用」被误读成「issue 没问题」。

约定：所有产出都必须带 `[未执行模型处理]` 前缀。
"""
from __future__ import annotations

from agents.base import (
    AnswerRequest,
    AnswerResult,
    IssueDigestRequest,
    IssueDigestResult,
    IssueDraft,
    IssueProgress,
    IssueReplyRequest,
)

TAG = "[未执行模型处理]"


class EchoBackend:
    """离线兜底后端。"""

    name = "echo"

    def available(self) -> tuple[bool, str]:
        # echo 始终可被选中，作为降级链的最后一环；但明确说明它不做任何模型处理。
        return True, (
            "echo 为离线兜底后端：不调用任何模型，所有产出仅带 "
            f"`{TAG}` 告警占位。请检查 AGENT_BACKEND 配置与依赖安装。"
        )

    def probe(self) -> str:
        return "echo 后端（离线，不调用模型）"

    def issue_digest(self, req: IssueDigestRequest) -> IssueDigestResult:
        """占位产出：为每条 issue 给一条明确标注「占位」的归纳，不抛错、不假装有结论。"""
        overview = (
            f"{TAG} 当前后端为 echo（离线兜底），无法归纳 issue 进展。"
            f"收到 {len(req.repos)} 个仓库、{len(req.issues)} 条 issue 的汇总请求"
            f"（回溯 {req.days} 天）。请检查 AGENT_BACKEND 配置与依赖安装。"
        )
        items = [
            IssueProgress(
                repo=m.repo,
                number=m.number,
                stage=f"{TAG} 未归纳",
                blockers="",
                recent="",
                next_step="请配置可用后端后重试（echo 不产出真实进展）。",
            )
            for m in req.issues
        ]
        return IssueDigestResult(overview=overview, items=items)

    def issue_reply(self, req: IssueReplyRequest) -> IssueDraft:
        return IssueDraft(
            summary=(
                f"{TAG} 当前后端为 echo（离线兜底），未对 "
                f"{req.repo} #{req.number} 做任何分析。请检查 AGENT_BACKEND 配置与依赖安装。"
            ),
            root_cause_hypothesis="",
            comment_md="",
            proposed_actions=[],
            confidence=0.0,
            references=[],
            open_questions=["echo 后端不会产出可用草稿，请配置可用后端后重试。"],
        )

    def answer(self, req: AnswerRequest) -> AnswerResult:
        return AnswerResult(
            text=(
                f"{TAG} 当前后端为 echo（离线兜底），无法回答知识库问题。"
                f"收到的问题：{req.question.strip() or '（空）'}。"
                "请检查 AGENT_BACKEND 配置与依赖安装。"
            ),
            references=[],
        )
