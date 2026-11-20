"""知识库抽象：引用/回答数据结构、索引状态与后端协议。

本服务不再内置 Agent/LLM；知识库只提供检索与 wiki 读取（`ask` 由 deepwiki 自身模型完成）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class Ref:
    """一条知识库/代码/提交的引用出处。"""

    kind: str  # "wiki" | "file" | "commit"
    path: str = ""
    url: str = ""
    snippet: str = ""


@dataclass
class AnswerResult:
    """一次知识库问答的结果。"""

    text: str = ""
    references: list[Ref] = field(default_factory=list)


@dataclass
class IndexStatus:
    """一个仓库的知识库索引状态。

    status 取值：indexed / not_indexed / indexing / failed / unknown / skipped。
    """

    repo: str
    status: str
    indexed_sha: str = ""
    detail: str = ""
    raw: dict = field(default_factory=dict)


@runtime_checkable
class RepoKnowledgeBase(Protocol):
    """仓库知识库后端协议。`repo` 一律为 `"owner/name"`。"""

    name: str

    def available(self) -> tuple[bool, str]:
        """（是否可用，原因）。不可用时上层应明确回报原因，不静默降级。"""
        ...

    def probe(self) -> str:
        """连通性自检，返回一句人类可读的描述；失败请抛异常。"""
        ...

    def index_status(self, repo: str) -> IndexStatus:
        """查询仓库索引状态。"""
        ...

    def ensure_indexed(self, repo: str, force: bool = False, token: str = "") -> IndexStatus:
        """确保仓库已索引：命中且 sha 未变则可跳过；force=True 时强制重建。"""
        ...

    def ask(self, repo: str, messages: list[dict], token: str = "") -> AnswerResult:
        """带历史的多轮问答，返回答案正文与出处。"""
        ...

    def retrieve(
        self, repo: str, query: str, top_k: int = 8, token: str = ""
    ) -> list[dict]:
        """只返回检索片段，不生成；每项 {path,startLine,endLine,title,text,score}。"""
        ...

    def get_wiki(self, repo: str, topic: str) -> str:
        """读取已生成 wiki 中某页的 markdown 正文；找不到返回空串（不编造）。"""
        ...
