"""Agent 后端的注册与发现。

- 内置后端：`pydantic_ai`（默认，M1 起启用）与 `echo`（离线兜底）。
- 第三方后端：在自有包里声明 entry point（group = `issue_master.agent_backends`），
  无需改动本仓库代码即可接入。
- 选择：`AGENT_BACKEND` → 不可用时按 `AGENT_FALLBACK` 链降级（非静默，会打印告警）。

注意：`pydantic_ai` 依赖（pydantic-ai）未安装时其 `available()` 返回不可用，
会自动落到 `echo`，并在日志里明确说明——绝不静默。内置后端一律以无参构造实例化，
重依赖（db / cipher / 知识库）由后端自身**懒构造**，不改变 `get_backend` 的签名与降级链。
"""
from __future__ import annotations

import importlib
import importlib.metadata
import logging
from typing import Any

from agents.base import AgentBackend
from config import Config

logger = logging.getLogger("issue_master.agents")

ENTRY_POINT_GROUP = "issue_master.agent_backends"

# 内置后端：名字 -> "模块路径:类名"
_BUILTIN_BACKENDS: dict[str, str] = {
    "pydantic_ai": "agents.backends.pydantic_ai_backend:PydanticAiBackend",
    "echo": "agents.backends.echo:EchoBackend",
}

_instances: dict[str, AgentBackend] = {}


def _load_class(spec: str) -> Any:
    module_path, _, attr = spec.partition(":")
    module = importlib.import_module(module_path)
    return getattr(module, attr)


def discover() -> dict[str, str]:
    """返回全部可用后端：名字 -> "模块路径:类名"（内置 + entry points）。"""
    found = dict(_BUILTIN_BACKENDS)
    try:
        entry_points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # noqa: BLE001 - 发现失败不应影响内置后端
        entry_points = []
    for ep in entry_points:
        if ep.value and ep.name not in found:
            found[ep.name] = ep.value
    return found


def list_backends() -> list[str]:
    """列出当前可发现的后端名（内置 + entry points），已排序。"""
    return sorted(discover())


def _instantiate(name: str) -> AgentBackend:
    if name in _instances:
        return _instances[name]
    specs = discover()
    if name not in specs:
        raise KeyError(f"未注册的后端 `{name}`（可选：{', '.join(sorted(specs))}）")
    cls = _load_class(specs[name])
    backend = cls()
    _instances[name] = backend
    return backend


def get_backend(name: str | None = None) -> AgentBackend:
    """按 `AGENT_BACKEND`（或显式 name）取后端，不可用则沿降级链下探。

    降级链：起点（AGENT_BACKEND 或 name）→ AGENT_FALLBACK 里的每一项。
    """
    requested = (name or Config.agent_backend or "echo").strip()
    chain = [requested] + [n for n in Config.agent_fallback if n and n != requested]

    problems: list[str] = []
    for candidate in chain:
        try:
            backend = _instantiate(candidate)
            ok, reason = backend.available()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{candidate} 加载失败：{exc}")
            continue
        if ok:
            if candidate != requested:
                logger.warning(
                    "后端 `%s` 不可用，已降级到 `%s`。（%s）",
                    requested,
                    candidate,
                    "；".join(problems),
                )
            return backend
        problems.append(f"{candidate} 不可用：{reason}")

    raise RuntimeError("没有可用的 Agent 后端 —— " + "；".join(problems))


def resolve_backend() -> AgentBackend:
    """按当前配置（`AGENT_BACKEND` → `AGENT_FALLBACK`）解析出可用后端。"""
    return get_backend()
