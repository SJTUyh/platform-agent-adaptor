"""Agent 工具：把知识库 / issue / 仓库 / 外部检索能力暴露给模型。

M1 阶段只提供 `kb_tools`（`kb_ask` / `kb_wiki`）；其余工具在后续里程碑补齐。
工具本身**不调用模型**，只做数据获取与纯文本整理。
"""
from agents.tools.kb_tools import KbToolset, build_tools

__all__ = ["KbToolset", "build_tools"]
