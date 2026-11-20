"""Pydantic AI 后端（默认后端，M1 起启用，M2 起支持 issue 巡检归纳，M3 起支持处理草稿）。

职责与约束：
- **唯一**允许 import 模型 SDK 的地方之一（另一个是 `agents/tools/`）。
- `_build_model()` **每次任务**都从 `store` 重新读取 `llm_config`（DB 优先，回退 .env），
  这样 `/llm set` 改完无需重启即生效；API Key 解密后只存在于内存。
- M1 实现 `answer()`（`/kb ask`）；M2 实现 `issue_digest()`（巡检归纳）；M3 实现
  `issue_reply()`（处理草稿，严格 JSON 解析，失败抛错不造假）。
- 未安装 `pydantic-ai` 时 `available()` 返回 `(False, 原因)`，由 registry 按降级链落到 echo。

注：本文件依赖 `pydantic-ai`，M1 环境若未安装则代码路径不会被走到。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from agents.base import (
    AgentBackend,
    AnswerRequest,
    AnswerResult,
    IssueDigestRequest,
    IssueDigestResult,
    IssueDraft,
    IssueProgress,
    IssueReplyRequest,
    Ref,
)
from config import Config

logger = logging.getLogger("issue_master.agents.pydantic_ai")

# 模型仅依据知识库作答，不得臆造
_SYSTEM_PROMPT = (
    "你是 issue 助手，负责依据代码仓库知识库回答研发人员的问题。"
    "结论必须以知识库 / 代码的真实内容为准：先调用 kb_ask 检索（必要时用 kb_wiki 读取页面），"
    "再据此作答；无法从知识库得到依据的，请明确说“知识库中未找到依据”，不要编造。"
    "回答用简体中文，尽量给出可核对的出处（文件路径 / 页面标题）。"
)

# 巡检归纳：只依据给定事实，不臆断「进展」；输出严格 JSON
_DIGEST_SYSTEM_PROMPT = (
    "你是 GitHub issue 巡检助手。下面会给你一批 issue 的**客观事实包**"
    "（state / labels / assignees / 评论数与最后评论时间 / timeline 关键事件 / 关联 PR）。"
    "请只依据这些事实，为每条 issue 归纳四件事："
    "当前阶段（stage）、卡点（blockers）、最近动向（recent）、建议下一步（next_step）。"
    "严禁编造事实包中不存在的信息；事实不足以判断时写「依据不足」。"
    "每条字段用简体中文，各不超过 60 字。"
    "输出必须是**严格的 JSON 对象**，不要输出解释、不要用 markdown 代码块，格式为："
    '{"overview": "全局概览一句话", '
    '"items": [{"repo": "owner/name", "number": 123, "stage": "", "blockers": "", '
    '"recent": "", "next_step": ""}]}'
)

# issue 处理草稿：只依据给定事实 + 知识库上下文产出评论与动作；输出严格 JSON
_HANDLE_SYSTEM_PROMPT = (
    "你是 GitHub issue 处理助手，需要产出一份经人工确认后可直接发布到 issue 下的处理草稿。"
    "你会收到一条 issue 的客观事实（标题 / 正文 / 评论 / timeline 事件）、"
    "发起人在飞书里给的额外提示词，以及知识库检索上下文（可能为空）。"
    "请只依据这些材料产出以下字段：\n"
    "- summary：一句话概括你的处理结论；\n"
    "- root_cause_hypothesis：根因假设，依据不足时写「依据不足」；\n"
    "- comment_md：**将发布到 issue 下的评论正文**（Markdown，简体中文，专业克制）；"
    "引用事实时必须给出真实出处（文件路径 / issue 编号 / 评论或 PR 链接）；"
    "无法核实的不要写进去；\n"
    "- proposed_actions：拟执行的**强副作用动作**（可为空数组），逐条列出，人工勾选后才执行；"
    "每项 kind 取 add_label/remove_label/assign/close/reopen，value 为标签名或用户名"
    "（close/reopen 的 value 留空），reason 为执行理由；\n"
    "- confidence：0~1 的置信度；\n"
    "- references：真实出处列表，kind 取 wiki/file/commit，path 或 url 至少一个非空；\n"
    "- open_questions：仍需人工确认的问题列表（可为空数组）。\n"
    "严禁编造事实包与知识库中不存在的信息。"
    "输出必须是**严格的 JSON 对象**，不要输出解释、不要用 markdown 代码块，格式为："
    '{"summary": "", "root_cause_hypothesis": "", "comment_md": "", '
    '"proposed_actions": [{"kind": "", "value": "", "reason": ""}], "confidence": 0.0, '
    '"references": [{"kind": "", "path": "", "url": "", "snippet": ""}], "open_questions": [""]}'
)

# 单次生成的 token 上限：本网关的推理模型把 reasoning_content 与正文计入**同一个**
# completion 预算（实测同一 prompt 的 reasoning 在 1162~8192+ token 间大幅波动）。
# 上限给小了会在正文写出前就被截断——表现为「模型返回中未找到 JSON 对象」或 content 为空。
# 注意：max_completion_tokens 只是「输出预算」，与模型上下文窗口不是一回事；给大不会让模型
# 多写，它写完 JSON 后会自然 stop，只在确实需要时才消耗。实测该网关接受 32768 且 finish=stop。
_DIGEST_MAX_TOKENS = 32768
# 处理草稿含较长的评论正文，token 上限同样留足
_HANDLE_MAX_TOKENS = 32768
# 喂给模型的正文 / 评论 / timeline 上限。这些上限只做「防超长撑爆 prompt」的兜底，
# **不承担「摘要」职责**：任一上限过小都会把后半段（异常栈、「实际行为」章节等）整体丢掉，
# 模型据此误判「正文/评论缺失」，因此取值明显放宽。
_BODY_MAX_CHARS = 8000      # 单条 issue 正文
_COMMENT_MAX_CHARS = 8000   # 单条评论正文
_MAX_HANDLE_COMMENTS = 40   # 喂给模型的评论条数
_MAX_HANDLE_TIMELINE = 60   # 喂给模型的 timeline 条数


class _DigestItem(BaseModel):
    """模型输出的单条 issue 归纳（映射到 `IssueProgress`）。"""

    repo: str
    number: int
    stage: str = ""
    blockers: str = ""
    recent: str = ""
    next_step: str = ""


class _DigestOutput(BaseModel):
    """模型输出的巡检归纳结果。"""

    overview: str = ""
    items: list[_DigestItem] = []


class _DraftAction(BaseModel):
    """模型输出的单条强副作用动作。"""

    kind: str = ""
    value: str = ""
    reason: str = ""


class _DraftRef(BaseModel):
    """模型输出的一条出处。"""

    kind: str = ""
    path: str = ""
    url: str = ""
    snippet: str = ""


class _DraftOutput(BaseModel):
    """模型输出的处理草稿（映射到 `IssueDraft`）。"""

    summary: str = ""
    root_cause_hypothesis: str = ""
    comment_md: str = ""
    proposed_actions: list[_DraftAction] = []
    confidence: float = 0.0
    references: list[_DraftRef] = []
    open_questions: list[str] = []


def _extract_json(text: str) -> dict:
    """从模型返回里抠出 JSON 对象（容忍前后解释文字、或被 ``` 围栏包裹的 JSON）。

    用 ``json.JSONDecoder.raw_decode`` 从**每个 ``{`` 起点**依次尝试解析，取第一个
    能解析成对象的结果：它能精确跳过 JSON 字符串内部的内容（例如 ``comment_md`` 里
    引用的 ```python 代码块），不会被「首个 ``` 围栏」劫持。此前用
    ``find/rfind`` + 非贪婪围栏正则的做法，一旦 JSON 字符串值内含围栏代码块
    （issue 正文常带 ```python 配置片段，模型又会在评论里引用它），就会把那段代码
    当作整体、丢掉真正的 JSON，表现为「模型返回中未找到 JSON 对象」。
    """
    raw = (text or "").strip()
    if not raw:
        raise RuntimeError("模型返回为空（content 为空字符串）——请检查 max_tokens 与网关配置")
    decoder = json.JSONDecoder()
    pos = raw.find("{")
    while pos >= 0:
        try:
            obj, _ = decoder.raw_decode(raw[pos:])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            return obj
        pos = raw.find("{", pos + 1)
    raise RuntimeError(f"模型返回中未找到 JSON 对象：{raw[:200]}")


class PydanticAiBackend:
    """基于 pydantic-ai 的 Agent 后端。"""

    name = "pydantic_ai"

    def __init__(self):
        self._db: Any = None
        self._cipher: Any = None
        self._engine: Any = None

    # ---------- 协议实现 ----------
    def available(self) -> tuple[bool, str]:
        try:
            import pydantic_ai  # noqa: F401
        except Exception as exc:  # noqa: BLE001 - 未安装依赖时按不可用处理
            return False, f"未安装 pydantic-ai（{exc}）；请 `pip install pydantic-ai-slim[openai]`"
        base_url, model, api_key, _ = self._llm_creds()
        if not model:
            return False, "LLM_MODEL 未配置"
        if not api_key:
            return False, "LLM_API_KEY 未配置（或 llm_config 中无 Key）"
        return True, ""

    def probe(self) -> str:
        """发一次最小对话验证连通性；失败抛异常。"""
        from pydantic_ai import Agent

        base_url, model, _, _ = self._llm_creds()
        agent = Agent(self._build_model(), system_prompt="你是连通性自检助手。")
        result = agent.run_sync("请只回复两个字：可用")
        text = self._result_text(result)
        return f"{model}@{base_url} 可用（返回：{text[:40] or '空'}）"

    def answer(self, req: AnswerRequest) -> AnswerResult:
        """`/kb ask` 的 Agent 化问答：挂 kb_ask / kb_wiki 工具，由模型据知识库作答。"""
        from pydantic_ai import Agent

        agent = Agent(
            self._build_model(),
            system_prompt=_SYSTEM_PROMPT,
            tools=self._kb_tools() if Config.agent_tools_enabled else [],
        )
        prompt = f"目标仓库：{req.repo}\n问题：{req.question}"
        if req.extra:
            prompt += f"\n补充说明：{req.extra}"
        result = agent.run_sync(prompt)
        # 出处由知识库工具在回答文本中给出；此处不臆造结构化 references。
        return AnswerResult(text=self._result_text(result).strip(), references=[])

    def issue_digest(self, req: IssueDigestRequest) -> IssueDigestResult:
        """巡检归纳：把事实包交给模型，产出结构化 `IssueProgress` 列表。

        只做一次纯文本调用 + JSON 解析（不依赖各版本 `output_type` / 工具调用），
        解析失败即抛错，由上层如实回报，不返回假结果。
        """
        from pydantic_ai import Agent

        agent = Agent(
            self._build_model(),
            system_prompt=_DIGEST_SYSTEM_PROMPT,
            model_settings=self._digest_settings(),
        )
        result = agent.run_sync(self._digest_prompt(req))
        text = self._result_text(result)
        try:
            data = _extract_json(text)
            parsed = _DigestOutput.model_validate(data)
        except (ValueError, TypeError, ValidationError, RuntimeError) as exc:
            logger.error("巡检归纳结果解析失败：%s", exc)
            raise RuntimeError(f"巡检归纳结果无法解析为结构化 JSON：{exc}") from exc

        items = [
            IssueProgress(
                repo=it.repo,
                number=int(it.number),
                stage=it.stage.strip(),
                blockers=it.blockers.strip(),
                recent=it.recent.strip(),
                next_step=it.next_step.strip(),
            )
            for it in parsed.items
        ]
        return IssueDigestResult(overview=parsed.overview.strip(), items=items)

    def issue_reply(self, req: IssueReplyRequest) -> IssueDraft:
        """issue 处理：把事实包 + 额外提示词 + 知识库上下文交给模型，产出结构化 `IssueDraft`。

        单次纯文本调用 + JSON 解析（与 `issue_digest` 同构），解析失败即抛错，
        由上层如实回报，不返回假草稿。
        """
        from pydantic_ai import Agent

        agent = Agent(
            self._build_model(),
            system_prompt=_HANDLE_SYSTEM_PROMPT,
            model_settings=self._handle_settings(),
        )
        result = agent.run_sync(self._handle_prompt(req))
        text = self._result_text(result)
        try:
            data = _extract_json(text)
            parsed = _DraftOutput.model_validate(data)
        except (ValueError, TypeError, ValidationError, RuntimeError) as exc:
            logger.error("issue 处理草稿解析失败：%s", exc)
            raise RuntimeError(f"issue 处理草稿无法解析为结构化 JSON：{exc}") from exc

        return IssueDraft(
            summary=parsed.summary.strip(),
            root_cause_hypothesis=parsed.root_cause_hypothesis.strip(),
            comment_md=parsed.comment_md.strip(),
            proposed_actions=[
                {"kind": a.kind.strip(), "value": a.value.strip(), "reason": a.reason.strip()}
                for a in parsed.proposed_actions
                if a.kind.strip()
            ],
            confidence=float(parsed.confidence or 0.0),
            references=[
                Ref(
                    kind=r.kind.strip(),
                    path=r.path.strip(),
                    url=r.url.strip(),
                    snippet=r.snippet.strip(),
                )
                for r in parsed.references
                if (r.path.strip() or r.url.strip() or r.snippet.strip())
            ],
            open_questions=[q.strip() for q in parsed.open_questions if q.strip()],
        )

    # ---------- 内部：配置与模型 ----------
    def _get_db(self):
        if self._db is None:
            from store.db import Database

            self._db = Database(Config.db_path)
        return self._db

    def _get_cipher(self):
        if self._cipher is None:
            from store.crypto import TokenCipher

            self._cipher = TokenCipher(Config.fernet_key)
        return self._cipher

    def _llm_creds(self) -> tuple[str, str, str, int]:
        """每次从 DB 取 LLM 配置（DB 优先，回退 .env）；Key 解密后仅在内存。"""
        try:
            row = self._get_db().get_llm_config()
        except Exception as exc:  # noqa: BLE001 - DB 不可用时回退 .env，不阻断
            logger.warning("读取 llm_config 失败，回退 .env：%s", exc)
            row = None
        if row is None:
            return Config.llm_base_url, Config.llm_model, Config.llm_api_key, Config.llm_timeout

        base_url = row["base_url"] or Config.llm_base_url
        model = row["model"] or Config.llm_model
        timeout = int(row["timeout"] or Config.llm_timeout)
        api_key = ""
        enc = row["api_key_enc"]
        if enc:
            try:
                api_key = self._get_cipher().decrypt(enc)
            except Exception as exc:  # noqa: BLE001 - 解密失败不暴露密文，提示后回退 .env
                logger.error("LLM API Key 解密失败（FERNET_KEY 是否与写入时一致？）：%s", exc)
                api_key = Config.llm_api_key
        else:
            api_key = Config.llm_api_key
        return base_url, model, api_key, timeout

    def _build_model(self):
        """按当前 LLM 配置构造 OpenAI 兼容模型（每次任务重建，支持热更新）。"""
        # pydantic-ai 2.x 将 OpenAIModel 更名为 OpenAIChatModel（兼容旧版本回退）。
        try:
            from pydantic_ai.models.openai import OpenAIChatModel as _ChatModel
        except ImportError:  # pragma: no cover - 旧版本兼容分支
            from pydantic_ai.models.openai import OpenAIModel as _ChatModel  # type: ignore
        from pydantic_ai.providers.openai import OpenAIProvider

        base_url, model, api_key, timeout = self._llm_creds()
        if not api_key:
            raise RuntimeError("LLM_API_KEY 未配置，无法构造模型（请 /llm set api_key 或检查 .env）")
        provider = OpenAIProvider(base_url=base_url, api_key=api_key)
        return _ChatModel(model, provider=provider)

    # ---------- 内部：知识库工具 ----------
    def _get_engine(self):
        """懒构造知识库编排层（供 kb_ask / kb_wiki 工具使用）。"""
        if self._engine is None:
            from adapters.github_api import build_github_api
            from engine.kb import KbEngine
            from knowledge import get_knowledge_base

            self._engine = KbEngine(
                self._get_db(), build_github_api(), get_knowledge_base()
            )
        return self._engine

    def _kb_tools(self) -> list[Any]:
        from agents.tools.kb_tools import KbToolset, build_tools

        return build_tools(KbToolset(self._get_engine()))

    # ---------- 内部：巡检归纳（事实包 → 提示词）----------
    @staticmethod
    def _digest_facts(meta) -> dict[str, Any]:
        """把一条 `IssueMeta` 事实包整理成喂给模型的紧凑 dict（只含客观字段）。"""
        return {
            "repo": meta.repo,
            "number": meta.number,
            "title": meta.title,
            "state": meta.state,
            "author": meta.author,
            "created_at": meta.created_at,
            "updated_at": meta.updated_at,
            "labels": list(meta.labels),
            "assignees": list(meta.assignees),
            "comments_count": meta.comments_count,
            "last_comment_at": meta.last_comment_at,
            "last_comment_author": meta.last_comment_author,
            "days_since_last_comment": meta.days_since_last_comment,
            "timeline_events": list(meta.timeline_events),
            "linked_prs": list(meta.linked_prs),
            "body": (meta.body or "")[:_BODY_MAX_CHARS],
        }

    def _digest_prompt(self, req: IssueDigestRequest) -> str:
        facts = [self._digest_facts(m) for m in req.issues]
        lines = [
            f"巡检范围：仓库 {', '.join(req.repos) or '（空）'}，回溯 {req.days} 天。",
            f"共 {len(facts)} 条 open issue 的事实包（JSON 数组）：",
            json.dumps(facts, ensure_ascii=False, indent=2),
        ]
        if req.extra:
            lines.append(f"补充说明：{req.extra}")
        lines.append(
            "请为上面每一条 issue 输出一条归纳（repo/number 必须与事实包一致），"
            "只输出前述约定的 JSON 对象。"
        )
        return "\n".join(lines)

    @staticmethod
    def _digest_settings():
        """巡检归纳的模型参数：留足 max_tokens（推理模型会先输出 reasoning_content）。"""
        from pydantic_ai import ModelSettings

        return ModelSettings(max_tokens=_DIGEST_MAX_TOKENS, temperature=0.2)

    # ---------- 内部：issue 处理（事实包 + 知识库 → 提示词）----------
    def _handle_prompt(self, req: IssueReplyRequest) -> str:
        lines = [
            f"仓库：{req.repo}　issue：#{req.number}　标题：{req.title or '（无标题）'}",
            "issue 正文：",
            # 上限放宽（原 2000）：过小的上限会把 issue 后半段（「实际行为」「期望行为」
            # 或异常栈）整体丢掉，模型据此误判「正文缺失/被截断」。此上限仅用于防止
            # 超长正文撑爆 prompt，不再承担「摘要」职责。
            (req.body or "").strip()[:_BODY_MAX_CHARS] or "（空）",
        ]
        comments = [c for c in (req.comments or []) if isinstance(c, dict)][-_MAX_HANDLE_COMMENTS:]
        if comments:
            lines.append(f"最近 {len(comments)} 条评论（时间正序）：")
            for idx, c in enumerate(comments, 1):
                actor = ((c.get("user") or {}).get("login") or "") if isinstance(c.get("user"), dict) else ""
                when = (c.get("created_at") or "")[:19]
                body = " ".join(str(c.get("body") or "").split())[:_COMMENT_MAX_CHARS]
                lines.append(f"  {idx}. [{when}] {actor or '（匿名）'}：{body}")
        else:
            lines.append("（无评论）")

        events = [e for e in (req.timeline or []) if isinstance(e, dict)][-_MAX_HANDLE_TIMELINE:]
        if events:
            lines.append("最近 timeline 事件（时间正序）：")
            for e in events:
                lines.append("  - " + self._timeline_line(e))

        lines.append(f"发起人额外提示词：{req.hint.strip() or '（未提供）'}")
        lines.append("知识库检索上下文：")
        lines.append(req.kb_context.strip() or "（知识库无返回）")
        lines.append("请只输出前述约定的 JSON 对象。")
        return "\n".join(lines)

    @staticmethod
    def _timeline_line(ev: dict) -> str:
        """把一条 timeline 事件压成人类可读短句（只取可解释的关键字段）。"""
        name = ev.get("event") or ""
        detail = ""
        if name in ("labeled", "unlabeled"):
            detail = (ev.get("label") or {}).get("name") or ""
        elif name in ("assigned", "unassigned"):
            detail = (ev.get("assignee") or {}).get("login") or ""
        elif name in ("milestoned", "demilestoned"):
            detail = (ev.get("milestone") or {}).get("title") or ""
        elif name in ("cross-referenced", "referenced"):
            src = (ev.get("source") or {}).get("issue") or {}
            kind = "PR" if "pull_request" in src else "issue"
            detail = f"#{src.get('number')}（{kind}）"
        actor = (ev.get("actor") or {}).get("login") or ""
        when = (ev.get("created_at") or "")[:19]
        parts = [name or "event"]
        if detail:
            parts.append(detail)
        if actor:
            parts.append(f"by {actor}")
        if when:
            parts.append(when)
        return " ".join(parts)

    @staticmethod
    def _handle_settings():
        """处理草稿的模型参数：留足 max_tokens（评论正文可能较长）。"""
        from pydantic_ai import ModelSettings

        return ModelSettings(max_tokens=_HANDLE_MAX_TOKENS, temperature=0.2)

    # ---------- 内部：结果抽取（兼容 pydantic-ai 各版本 output/data 命名）----------
    @staticmethod
    def _result_text(result: Any) -> str:
        for attr in ("output", "data"):
            value = getattr(result, attr, None)
            if isinstance(value, str):
                return value
            if value is not None:
                return str(value)
        return ""
