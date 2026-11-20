"""Agent 层：所有「需要模型处理」的信息都经由这里。

对外只暴露 registry 的解析函数；业务代码不要直接 import 任何 LLM SDK。
"""
from agents.base import (
    AgentBackend,
    AnswerRequest,
    AnswerResult,
    IssueDigestRequest,
    IssueDigestResult,
    IssueDraft,
    IssueMeta,
    IssueProgress,
    IssueReplyRequest,
    Ref,
)
from agents.registry import get_backend, list_backends, resolve_backend

__all__ = [
    "AgentBackend",
    "AnswerRequest",
    "AnswerResult",
    "IssueDigestRequest",
    "IssueDigestResult",
    "IssueDraft",
    "IssueMeta",
    "IssueProgress",
    "IssueReplyRequest",
    "Ref",
    "get_backend",
    "list_backends",
    "resolve_backend",
]
