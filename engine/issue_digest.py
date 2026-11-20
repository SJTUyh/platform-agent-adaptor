"""issue 巡检编排层：逐仓库拉事实 → 组装事实包 → 转交 Agent → 分组渲染卡片结构。

只做「拉数据 → 组装请求 → 转交 Agent → 渲染」，**不直接调用任何模型**（模型调用一律走 `agents`）。

事实判据全部来自 GitHub 客观返回（`state` / `labels` / `assignees` / 评论数与最后评论时间 /
timeline 关键事件 / 关联 PR），模型只负责把事实归纳成「当前阶段 / 卡点 / 最近动向 / 建议下一步」。

契合 PLAN §6.6 的名义接口：`collect()` / `summarize()` / `render()`。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from agents.base import IssueDigestRequest, IssueDigestResult, IssueMeta
from config import Config

logger = logging.getLogger("issue_master.digest")

# 飞书交互卡片单卡元素数上限（PLAN §6.6）；超出要截断并在卡片里注明
CARD_MAX_ELEMENTS = 20
# 一个 issue 块占用的元素数：正文 div + 动作按钮行 action（见 adapters/feishu_bot.card_issue_digest）
CARD_ELEMENTS_PER_ISSUE = 2
# 归类展示分组
GROUP_NEW = "new"
GROUP_PROGRESS = "progress"
GROUP_STALL = "stall"
GROUP_ORDER = [GROUP_NEW, GROUP_PROGRESS, GROUP_STALL]
GROUP_TITLES = {
    GROUP_NEW: "① 新增未响应",
    GROUP_PROGRESS: "② 进行中",
    GROUP_STALL: "③ 停滞",
}
WARN_GROUP_TITLE = "④ ⚠️ 拉取失败仓库"
# timeline 里保留的关键事件条数（仅限制喂给模型的细节量，不影响「是否有人处置」判定）
_MAX_TIMELINE_EVENTS = 30
# 单条 issue 的关联 PR 上限（同上，仅限制喂给模型的细节量）
_MAX_LINKED_PRS = 20

# 视为「有人处置过」的 timeline 事件（用于「新增未响应」判定）
_ACTION_EVENTS = {
    "labeled",
    "unlabeled",
    "assigned",
    "unassigned",
    "milestoned",
    "demilestoned",
    "cross-referenced",
    "referenced",
    "reopened",
    "closed",
}
# timeline 事件 -> 中文短句前缀
_EVENT_LABEL = {
    "labeled": "打标签",
    "unlabeled": "移除标签",
    "assigned": "指派",
    "unassigned": "取消指派",
    "milestoned": "加入里程碑",
    "demilestoned": "移出里程碑",
    "closed": "关闭",
    "reopened": "重新打开",
    "referenced": "引用",
    "cross-referenced": "关联",
}
# 停滞细分三类（PLAN §5.2 / §九 已定第 2 条）
STALL_ASSIGNED = "stale_assigned"        # ① 有 assignee 但 ≥N 天无评论
STALL_UNASSIGNED = "stale_unassigned"    # ② 无 assignee 且 ≥N 天无评论
STALL_LINKED_PR = "stale_linked_pr"      # ③ 关联 PR 未合并且长期无更新


# ---------------- 渲染结构（供 app/main.py 转成飞书卡片 JSON）----------------

@dataclass
class DigestBlock:
    """一条 issue 的卡片块：事实依据 + 模型归纳 + 归类分组。"""

    repo: str
    number: int
    title: str
    url: str
    group: str
    reason: str = ""            # 归类的事实依据（必填语义）
    stall_kind: str = ""        # 停滞细分三类
    stage: str = ""
    blockers: str = ""
    recent: str = ""
    next_step: str = ""
    assignees: list[str] = field(default_factory=list)
    author: str = ""            # issue 创建人（GitHub login）
    labels: list[str] = field(default_factory=list)
    comments_count: int = 0
    created_date: str = ""
    last_activity_date: str = ""
    stale_days: int = -1        # 距最后动向的天数（-1=不可解析）


@dataclass
class DigestGroup:
    """一个已渲染的分组（blocks 已按元素预算截断）。"""

    key: str
    title: str
    blocks: list[DigestBlock] = field(default_factory=list)


@dataclass
class DigestView:
    """渲染结果：可直接交给 `adapters.feishu_bot.card_issue_digest` 转成卡片 JSON。"""

    repos: list[str]
    days: int
    groups: list[DigestGroup] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)   # 各分组截断前的完整条数
    warnings: list[str] = field(default_factory=list)      # 全部拉取失败项（④）
    total_issues: int = 0
    truncated: int = 0
    stall_days: int = 0
    overview: str = ""
    warnings_in_card: bool = False                          # 是否给 ④ 单独分配了卡片元素


# ---------------- 时间工具 ----------------

def _parse_ts(ts: str) -> datetime | None:
    """解析 GitHub 的 ISO8601 时间戳（形如 2026-10-16T09:30:00Z）。"""
    raw = (ts or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _days_since(ts: str, now: datetime) -> int:
    """距指定时间戳的天数（向下取整）；不可解析返回 -1。"""
    dt = _parse_ts(ts)
    if dt is None:
        return -1
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int((now - dt).total_seconds() // 86400))


def _window(days: int, now: datetime | None = None) -> tuple[datetime, str]:
    """回溯窗口：返回 (起始时刻, 传给 GitHub `since` 的 ISO8601)。"""
    now = now or datetime.now(timezone.utc)
    days = max(1, int(days))
    since_dt = now - timedelta(days=days)
    return since_dt, since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def window_since(days: int, now: datetime | None = None) -> str:
    """回溯窗口起始时刻（ISO8601），供 `issue-poller` 等复用同一口径。"""
    return _window(days, now)[1]


# ---------------- 巡检编排 ----------------

class IssueDigestEngine:
    """issue 巡检：拉事实 → 转交 Agent → 分组渲染。"""

    def __init__(self, github, backend, stall_days: int | None = None):
        self.github = github
        self.backend = backend
        self.stall_days = max(1, int(stall_days if stall_days is not None else Config.issue_stall_days))

    # ---------- 1. 拉取事实 ----------
    def collect(
        self, repos: list[str], days: int, now: datetime | None = None
    ) -> tuple[list[IssueMeta], list[str]]:
        """逐仓库拉取并组装事实包；**单仓库失败不影响其它仓库**，失败项进 warnings。"""
        now = now or datetime.now(timezone.utc)
        _, since_iso = _window(days, now)
        facts: list[IssueMeta] = []
        warnings: list[str] = []
        for repo in repos:
            try:
                items = self.github.list_recent_issues(repo, since_iso)
            except Exception as exc:  # noqa: BLE001 - 单仓库失败不得中断整体巡检
                msg = f"{repo}：拉取失败（{exc}）"
                warnings.append(msg)
                logger.warning("巡检拉取失败：%s", msg)
                continue
            logger.info("巡检拉取 %s：%d 条 open issue（since=%s）", repo, len(items), since_iso)
            for item in items:
                try:
                    facts.append(self._build_fact(repo, item, now))
                except Exception as exc:  # noqa: BLE001 - 单条 issue 失败不中断该仓库
                    msg = f"{repo}#{(item or {}).get('number')}：事实包组装失败（{exc}）"
                    warnings.append(msg)
                    logger.warning("%s", msg)
        return facts, warnings

    def _build_fact(self, repo: str, item: dict, now: datetime) -> IssueMeta:
        """把一条 issue 的 GitHub 原始数据 + 评论 + timeline 组装成事实包。"""
        number = int(item.get("number"))
        comments: list[dict] = []
        timeline: list[dict] = []
        # 评论 / timeline 单独容错：拉不到时按「未知」处理，不臆断为「无动向」
        comments_ok = True
        try:
            comments = self.github.get_issue_comments(repo, number)
        except Exception as exc:  # noqa: BLE001
            comments_ok = False
            logger.warning("拉取 %s#%s 评论失败：%s", repo, number, exc)
        try:
            timeline = self.github.get_issue_timeline(repo, number)
        except Exception as exc:  # noqa: BLE001
            logger.warning("拉取 %s#%s timeline 失败：%s", repo, number, exc)

        comments_count = int(item.get("comments") or 0)
        last_comment = comments[-1] if comments else {}
        last_comment_at = (last_comment.get("created_at") or "") if last_comment else ""
        created_at = item.get("created_at") or ""
        updated_at = item.get("updated_at") or ""
        # 无动向起算点：优先最后评论时间；无评论时用创建时间；
        # 评论拉取失败但确实有评论时，退化为「最近更新时间」近似（渲染时注明）
        if last_comment_at:
            base_at = last_comment_at
        elif comments_count == 0 or comments_ok:
            base_at = created_at
        else:
            base_at = updated_at

        return IssueMeta(
            repo=repo,
            number=number,
            title=item.get("title") or "",
            author=((item.get("user") or {}).get("login") or ""),
            html_url=item.get("html_url") or "",
            created_at=created_at,
            updated_at=updated_at,
            state=item.get("state") or "open",
            labels=[(l or {}).get("name") or "" for l in (item.get("labels") or []) if isinstance(l, dict)],
            assignees=[
                (a or {}).get("login") or ""
                for a in (item.get("assignees") or [])
                if isinstance(a, dict)
            ],
            comments_count=comments_count,
            last_comment_at=last_comment_at,
            last_comment_author=((last_comment.get("user") or {}).get("login") or ""),
            body=item.get("body") or "",
            timeline_events=self._summarize_timeline(timeline),
            linked_prs=self._linked_prs(repo, number, timeline),
            days_since_last_comment=_days_since(base_at, now),
        )

    def _summarize_timeline(self, timeline: list[dict]) -> list[str]:
        """把 timeline 里可解释的关键事件压成人类可读短句（保留最近若干条）。"""
        out: list[str] = []
        for ev in timeline or []:
            if not isinstance(ev, dict):
                continue
            name = ev.get("event") or ""
            label = _EVENT_LABEL.get(name)
            if not label:
                continue
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
            elif name == "closed":
                detail = ev.get("commit_id") or ""
            actor = (ev.get("actor") or {}).get("login") or ""
            text = f"{name}: {label}" + (f" {detail}" if detail else "") + (f"（{actor}）" if actor else "")
            out.append(text)
        return out[-_MAX_TIMELINE_EVENTS:]

    def _linked_prs(self, repo: str, number: int, timeline: list[dict]) -> list[dict]:
        """关联 PR：优先 timeline 的 cross-referenced，拿不到时用搜索接口兜底。"""
        prs: list[dict] = []
        for ev in timeline or []:
            if not isinstance(ev, dict) or ev.get("event") != "cross-referenced":
                continue
            src = (ev.get("source") or {}).get("issue") or {}
            if "pull_request" not in src or not src.get("number"):
                continue
            pr_obj = src.get("pull_request") or {}
            prs.append(
                {
                    "number": int(src["number"]),
                    "url": src.get("html_url") or "",
                    "state": src.get("state") or "",
                    "merged": (bool(pr_obj.get("merged_at")) if "merged_at" in pr_obj else None),
                    "updated_at": src.get("updated_at") or "",
                    "source": "timeline",
                }
            )
        if not prs:
            try:
                for item in self.github.search_linked_prs(repo, number):
                    text = f"{item.get('title') or ''}\n{item.get('body') or ''}"
                    if f"#{number}" not in text:
                        continue  # 搜索命中的可能是无关 PR，做一次廉价过滤
                    pr_obj = item.get("pull_request") or {}
                    prs.append(
                        {
                            "number": int(item.get("number")),
                            "url": item.get("html_url") or "",
                            "state": item.get("state") or "",
                            "merged": bool(pr_obj.get("merged_at")),
                            "updated_at": item.get("updated_at") or "",
                            "source": "search",
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - 兜底失败不影响事实包主体
                logger.warning("关联 PR 兜底搜索失败（%s#%s）：%s", repo, number, exc)

        out: list[dict] = []
        seen: set[int] = set()
        for pr in prs:
            if pr["number"] in seen:
                continue
            seen.add(pr["number"])
            out.append(pr)
        return out[:_MAX_LINKED_PRS]

    # ---------- 2. 转交 Agent 归纳 ----------
    def summarize(
        self,
        repos: list[str],
        days: int,
        facts: list[IssueMeta],
        extra: str = "",
    ) -> IssueDigestResult:
        """把事实包交给 `agents` 层的 `issue_digest()`（此处不直接调模型）。

        无 issue 时不调用模型（省成本、避免空转臆造），直接返回空结果。
        """
        if not facts:
            return IssueDigestResult(overview="", items=[])
        req = IssueDigestRequest(repos=list(repos), days=int(days), issues=list(facts), extra=extra)
        return self.backend.issue_digest(req)

    # ---------- 3. 分组渲染 ----------
    def render(
        self,
        result: IssueDigestResult,
        facts: list[IssueMeta],
        warnings: list[str],
        repos: list[str],
        days: int,
        now: datetime | None = None,
    ) -> DigestView:
        """按事实归类分组，并在卡片元素预算内截断（超出的在卡片头注明）。"""
        now = now or datetime.now(timezone.utc)
        since_dt, _ = _window(days, now)
        progress_by_key = {(p.repo, p.number): p for p in result.items}

        grouped: dict[str, list[DigestBlock]] = {k: [] for k in GROUP_ORDER}
        for fact in facts:
            group, stall_kind, reason = self._classify(fact, since_dt)
            prog = progress_by_key.get((fact.repo, fact.number))
            grouped[group].append(
                DigestBlock(
                    repo=fact.repo,
                    number=fact.number,
                    title=fact.title,
                    url=fact.html_url,
                    group=group,
                    reason=reason,
                    stall_kind=stall_kind,
                    stage=(prog.stage if prog else "（模型未给出归纳）"),
                    blockers=(prog.blockers if prog else ""),
                    recent=(prog.recent if prog else ""),
                    next_step=(prog.next_step if prog else ""),
                    assignees=list(fact.assignees),
                    author=fact.author,
                    labels=list(fact.labels),
                    comments_count=fact.comments_count,
                    created_date=(fact.created_at or "")[:10],
                    last_activity_date=(fact.last_comment_at or fact.updated_at or "")[:10],
                    stale_days=fact.days_since_last_comment,
                )
            )

        # 分组内排序：新增按创建时间倒序；进行中按最近动向倒序；停滞按停滞天数倒序
        grouped[GROUP_NEW].sort(key=lambda b: b.created_date, reverse=True)
        grouped[GROUP_PROGRESS].sort(key=lambda b: b.last_activity_date, reverse=True)
        grouped[GROUP_STALL].sort(key=lambda b: b.stale_days, reverse=True)

        totals = {k: len(v) for k, v in grouped.items()}
        total_issues = sum(totals.values())

        # 元素预算：1 个汇总头 + 每组「标题 + N*2」+ 可选 ④（标题 + 1 个汇总块）
        budget = CARD_MAX_ELEMENTS - 1
        warnings_in_card = False
        if warnings and budget >= 2:
            budget -= 2
            warnings_in_card = True

        groups: list[DigestGroup] = []
        placed = 0
        for key in GROUP_ORDER:
            items = grouped[key]
            if not items:
                continue
            if budget < 1 + CARD_ELEMENTS_PER_ISSUE:
                break  # 放不下「标题 + 至少 1 块」
            budget -= 1
            take = min(len(items), budget // CARD_ELEMENTS_PER_ISSUE)
            budget -= take * CARD_ELEMENTS_PER_ISSUE
            groups.append(DigestGroup(key=key, title=GROUP_TITLES[key], blocks=items[:take]))
            placed += take

        return DigestView(
            repos=list(repos),
            days=int(days),
            groups=groups,
            totals=totals,
            warnings=list(warnings),
            total_issues=total_issues,
            truncated=max(0, total_issues - placed),
            stall_days=self.stall_days,
            overview=(result.overview or ""),
            warnings_in_card=warnings_in_card,
        )

    # ---------- 归类判据（纯事实）----------
    def _classify(self, fact: IssueMeta, since_dt: datetime) -> tuple[str, str, str]:
        """返回 (分组, 停滞细分类型, 事实依据字符串)；判据全部来自客观字段。

        优先级：停滞（≥N 天无动向）> 新增未响应 > 进行中。
        """
        created = _parse_ts(fact.created_at)
        is_new = created is not None and created >= since_dt
        days = fact.days_since_last_comment
        stale = days >= 0 and days >= self.stall_days
        responded = fact.comments_count > 0 or self._has_action_event(fact)

        if stale:
            kind, reason = self._stall_reason(fact)
            return GROUP_STALL, kind, reason
        if is_new and not responded:
            return (
                GROUP_NEW,
                "",
                f"新增未响应：{(fact.created_at or '')[:10]} 创建，{self._base_days_text(fact)}"
                f"，无评论、无处置事件",
            )
        if is_new:
            return (
                GROUP_PROGRESS,
                "",
                f"新增但已有响应：{fact.comments_count} 条评论，{self._base_days_text(fact)}",
            )
        if days < 0:
            return GROUP_PROGRESS, "", "时间戳不可解析，未判为停滞（需人工确认）"
        return (
            GROUP_PROGRESS,
            "",
            f"进行中：{self._base_days_text(fact)}（未超停滞阈值 {self.stall_days} 天）",
        )

    def _base_days_text(self, fact: IssueMeta) -> str:
        """「无动向多久」的事实描述（区分有无评论、评论时间是否可取）。"""
        days = fact.days_since_last_comment
        if fact.last_comment_at:
            return f"距最后评论 {days} 天"
        if fact.comments_count > 0:
            return f"评论时间不可取（按最近更新时间近似），{days} 天无动向"
        return f"自创建后 {days} 天无评论"

    def _stall_reason(self, fact: IssueMeta) -> tuple[str, str]:
        """停滞细分：① 有 assignee ② 关联 PR 未合并 ③ 无 assignee（判据全为事实）。"""
        base = self._base_days_text(fact)
        pr = self._stale_unmerged_pr(fact)
        if fact.assignees:
            reason = f"有 assignee（{'、'.join(fact.assignees)}），{base}（≥{self.stall_days} 天）"
            if pr:
                reason += f"；另有未合并关联 PR #{pr['number']}"
            return STALL_ASSIGNED, reason
        if pr:
            return (
                STALL_LINKED_PR,
                f"关联 PR #{pr['number']} 未合并（state={pr.get('state') or '未知'}），"
                f"{base}（≥{self.stall_days} 天）",
            )
        return STALL_UNASSIGNED, f"无 assignee，{base}（≥{self.stall_days} 天）"

    def _stale_unmerged_pr(self, fact: IssueMeta) -> dict | None:
        """找一条「未合并且长期无更新」的关联 PR（merged 未知时按 state 判定）。"""
        for pr in fact.linked_prs:
            if pr.get("merged") is True:
                continue
            if (pr.get("state") or "open") != "open":
                continue
            pr_days = _days_since(pr.get("updated_at") or "", datetime.now(timezone.utc))
            if pr_days < 0 or pr_days >= self.stall_days:
                return pr
        return None

    @staticmethod
    def _has_action_event(fact: IssueMeta) -> bool:
        """timeline 里是否有「有人处置过」的事件（短句以 `<event>: ` 开头）。"""
        return any(
            (e.split(":", 1)[0].strip() in _ACTION_EVENTS) for e in (fact.timeline_events or [])
        )

    # ---------- 便捷入口（/issues 与定时汇报共用）----------
    def run(
        self, repos: list[str], days: int, extra: str = "", now: datetime | None = None
    ) -> tuple[DigestView, list[IssueMeta], list[str]]:
        """一次完整巡检：collect → summarize → render。异常由调用方如实回报。"""
        facts, warnings = self.collect(repos, days, now=now)
        result = self.summarize(repos, days, facts, extra=extra)
        view = self.render(result, facts, warnings, repos, days, now=now)
        return view, facts, warnings


def as_dict(view: DigestView) -> dict[str, Any]:
    """把渲染结果转成普通 dict（便于日志与离线验证打印）。"""
    return {
        "repos": view.repos,
        "days": view.days,
        "total_issues": view.total_issues,
        "truncated": view.truncated,
        "stall_days": view.stall_days,
        "overview": view.overview,
        "warnings": view.warnings,
        "groups": [
            {
                "key": g.key,
                "title": g.title,
                "count": view.totals.get(g.key, len(g.blocks)),
                "blocks": [b.__dict__ for b in g.blocks],
            }
            for g in view.groups
        ],
    }
