"""知识库工具：`kb_ask` / `kb_wiki`。

- 走 `engine.kb.KbEngine` 编排层（缓存 + 多轮 + 状态落库），不直接碰 HTTP。
- 返回值一律是**纯文本**，便于直接塞进模型上下文；本模块不调用任何模型。
- 出处缺失时明确写「未返回出处」，绝不编造。
"""
from __future__ import annotations

import logging
from typing import Any

from agents.base import AnswerResult
from engine.kb import KbEngine

logger = logging.getLogger("issue_master.agents.tools.kb")

# Agent 内部调用知识库时使用的固定会话标识（与飞书群 chat_id 区分开）
_TOOL_CHAT_ID = "agent"


class KbToolset:
    """知识库工具集：构造时注入编排层 `KbEngine`。"""

    def __init__(self, engine: KbEngine):
        self.engine = engine

    def kb_ask(self, repo: str, question: str) -> str:
        """向指定仓库的知识库提问，返回带引用出处的纯文本答案。

        repo 形如 `owner/name`；question 为自然语言问题。
        """
        ok, reason = self._available()
        if not ok:
            return f"知识库不可用：{reason}"
        try:
            answer = self.engine.ask(_TOOL_CHAT_ID, repo, question)
        except Exception as exc:  # noqa: BLE001 - 失败必须如实回报，不静默
            logger.warning("kb_ask 调用失败（repo=%s）：%s", repo, exc)
            return f"知识库查询失败：{exc}"
        return self._format_answer(repo, answer)

    def kb_wiki(self, repo: str, topic: str) -> str:
        """读取仓库知识库中某个 wiki 页面的正文（按标题/主题匹配）。

        找不到时返回「未找到该 wiki 页面」，不编造内容。
        """
        ok, reason = self._available()
        if not ok:
            return f"知识库不可用：{reason}"
        try:
            text = self.engine.kb.get_wiki(repo, topic)
        except Exception as exc:  # noqa: BLE001 - 失败必须如实回报，不静默
            logger.warning("kb_wiki 调用失败（repo=%s, topic=%s）：%s", repo, topic, exc)
            return f"知识库读取失败：{exc}"
        text = (text or "").strip()
        if not text:
            return f"未找到该 wiki 页面（repo={repo}，topic={topic}）。"
        return text

    # ---------------- 内部 ----------------
    def _available(self) -> tuple[bool, str]:
        try:
            return self.engine.kb.available()
        except Exception as exc:  # noqa: BLE001
            return False, f"检查知识库可用性失败：{exc}"

    @staticmethod
    def _format_answer(repo: str, answer: AnswerResult) -> str:
        text = (answer.text or "").strip()
        lines: list[str] = [f"[{repo}] {text}" if text else f"[{repo}] 知识库未返回内容。"]
        refs = answer.references
        if refs:
            lines.append("出处：")
            for ref in refs:
                loc = ref.path or ref.url or "(未知位置)"
                tail = f"（{ref.url}）" if ref.url and ref.url != loc else ""
                lines.append(f"- [{ref.kind or 'ref'}] {loc}{tail}")
        else:
            lines.append("出处：未返回出处。")
        return "\n".join(lines)


def build_tools(toolset: KbToolset) -> list[Any]:
    """把 `KbToolset` 的方法整理成工具列表（供后端注册）。

    返回绑定方法列表；真正接入具体 Agent 框架时由后端自行包装。
    """
    return [toolset.kb_ask, toolset.kb_wiki]
