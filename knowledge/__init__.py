"""知识库适配层：按 `KB_BACKEND` 选择后端，对外暴露统一接口。

当前只实现 `deepwiki_open`（自托管 deepwiki-open）。对未实现的后端**抛清晰的中文错误**，
不做静默降级——避免让人误以为换后端成功了。
"""
from __future__ import annotations

from config import Config
from knowledge.base import IndexStatus, RepoKnowledgeBase
from knowledge.deepwiki_open import DeepWikiOpenKB

# 内置后端：名字 -> 构造器
_BUILTIN_BACKENDS: dict[str, type] = {
    "deepwiki_open": DeepWikiOpenKB,
}

_instances: dict[str, RepoKnowledgeBase] = {}


def list_knowledge_backends() -> list[str]:
    """列出当前已实现的知识库后端名（已排序）。"""
    return sorted(_BUILTIN_BACKENDS)


def get_knowledge_base(name: str | None = None) -> RepoKnowledgeBase:
    """按 `KB_BACKEND`（或显式 name）取知识库后端实例。

    未实现的后端会抛出 `NotImplementedError`（清晰中文说明），不静默降级。
    """
    backend = (name or Config.kb_backend or "deepwiki_open").strip()
    if backend in _instances:
        return _instances[backend]
    if backend not in _BUILTIN_BACKENDS:
        raise NotImplementedError(
            f"知识库后端 `{backend}` 尚未实现（当前可选：{', '.join(list_knowledge_backends())}）。"
            "请在 KB_BACKEND 中改用已实现的后端；本项目不会静默降级到其它后端。"
        )
    instance = _BUILTIN_BACKENDS[backend]()
    _instances[backend] = instance
    return instance


__all__ = [
    "IndexStatus",
    "RepoKnowledgeBase",
    "DeepWikiOpenKB",
    "get_knowledge_base",
    "list_knowledge_backends",
]
