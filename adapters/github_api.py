"""极简 GitHub REST 客户端：供启动自检（连通性 / 写权限探测）与 issue 只读。

M0 只需两件事：
1. 探测机器人身份是否可用、是否对目标仓库有 issue 写权限；
2. issue 只读拉取（`list_issues` / `get_issue`），供 M2 巡检使用。

回写身份支持两种模式（见 PLAN §6.1）：
- `app`：GitHub App，用私钥签发 RS256 JWT → 换取 installation token（1h，自动轮换）；
- `pat`：机器人 PAT，直接用环境变量里的 token。

注：issue 写操作（发表评论 / 加标签 / 指派 / 关闭）**只在 M3 的人工确认后**由编排层调用，
本模块只提供原子方法，自身不做任何「自动发布」决策。
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from typing import Any, Callable
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import Config

logger = logging.getLogger("issue_master.github")

# 单仓库单次最多拉取的 issue 数（避免刷屏与模型成本失控）
_MAX_ISSUES_PER_REPO = 100
# 单个 issue 最多回看的评论 / timeline 条数（均取「最近」的一页）：取值放宽以免
# 关键信息（异常栈等）落在被丢掉的老评论里；这仍是「防爆」兜底，页码级仍只取最近一页。
_MAX_COMMENTS_PER_ISSUE = 100
_MAX_TIMELINE_PER_ISSUE = 100
# 关联 PR 兜底搜索最多取几条
_MAX_LINKED_PRS = 20
# timeline 端点旧版要求 preview 媒体类型（兼容 GitHub Enterprise 旧版本）
_TIMELINE_ACCEPT = "application/vnd.github.mockingbird-preview+json"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_app_jwt(app_id: str, private_key_pem: bytes) -> str:
    """用 GitHub App 私钥签发一个短时 RS256 JWT（有效期 ≤10 分钟）。"""
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": now - 60, "exp": now + 540, "iss": str(app_id)}
    segments = [
        _b64url(json.dumps(header, separators=(",", ":")).encode()),
        _b64url(json.dumps(payload, separators=(",", ":")).encode()),
    ]
    signing_input = ".".join(segments).encode("ascii")
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    segments.append(_b64url(signature))
    return ".".join(segments)


class _RetryingClient(httpx.Client):
    """GET 瞬时网络错误自动重试。

    巡检走「GitHub → gost 代理（MITM CA）→ 本机」，TLS 握手中断（SSL EOF）与握手超时
    属高频抖动；这类错误是瞬时且幂等的，重试即可自愈。只重试 GET，写操作仍单次执行，
    避免重复发表评论等副作用。
    """

    retries: int = 3
    backoff: float = 0.5

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        last: httpx.TransportError | None = None
        for attempt in range(1, self.retries + 1):
            try:
                return super().get(url, **kwargs)
            except httpx.TransportError as exc:
                last = exc
                if attempt < self.retries:
                    logger.warning("GET %s 瞬时失败（%s），第 %d 次重试", url, exc, attempt)
                    time.sleep(self.backoff * attempt)
        assert last is not None
        logger.error("GET %s 重试 %d 次仍失败：%s", url, self.retries, last)
        raise last


class GitHubAppAuth:
    """GitHub App 认证：签发 installation token 并缓存至过期前。"""

    def __init__(self, app_id: str, private_key_path: str, installation_id: str, api_base: str):
        self.app_id = app_id
        self.private_key_path = private_key_path
        self.installation_id = installation_id
        self.api_base = api_base.rstrip("/")
        self._token: str | None = None
        self._exp: float = 0.0
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=30)

    def _private_key_pem(self) -> bytes:
        with open(self.private_key_path, "rb") as fh:
            return fh.read()

    def installation_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._exp - 60:
                return self._token
            jwt = _make_app_jwt(self.app_id, self._private_key_pem())
            r = self._client.post(
                f"{self.api_base}/app/installations/{self.installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "Accept": "application/vnd.github+json",
                },
            )
            r.raise_for_status()
            data = r.json()
            self._token = data["token"]
            # expires_at 形如 "2024-01-01T00:00:00Z"；解析失败则保守按 55 分钟缓存
            exp = data.get("expires_at", "")
            try:
                self._exp = time.mktime(time.strptime(exp, "%Y-%m-%dT%H:%M:%SZ")) + 8 * 3600
            except (ValueError, TypeError):
                self._exp = time.time() + 55 * 60
            return self._token

    def installation_permissions(self) -> dict:
        """读 installation 被实际授予的权限（需 App JWT，而非 installation token）。"""
        jwt = _make_app_jwt(self.app_id, self._private_key_pem())
        r = self._client.get(
            f"{self.api_base}/app/installations/{self.installation_id}",
            headers={
                "Authorization": f"Bearer {jwt}",
                "Accept": "application/vnd.github+json",
            },
        )
        r.raise_for_status()
        return r.json().get("permissions") or {}


class GitHubApi:
    """issue 读写 + 连通性/权限探测。token 由外部 provider 提供。

    读方法供巡检用；写方法（post_comment / add_labels / assign / close …）
    仅在 M3 人工确认后由 `engine.issue_handle` 调用。
    """

    def __init__(
        self,
        api_base: str,
        token_provider: Callable[[], str],
        installation_permissions: Callable[[], dict] | None = None,
    ):
        self.api_base = api_base.rstrip("/")
        self._token_provider = token_provider
        # app 模式：提供 installation 权限回读（issue 写权限以此为准）
        self._installation_permissions = installation_permissions
        self._client = _RetryingClient(timeout=30)

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        """默认 `application/vnd.github+json`；个别端点（timeline）需覆盖媒体类型。"""
        return {
            "Authorization": f"Bearer {self._token_provider()}",
            "Accept": accept,
        }

    # ---------- 探测 ----------
    def get_repo(self, repo: str) -> dict:
        r = self._client.get(f"{self.api_base}/repos/{repo}", headers=self._headers())
        r.raise_for_status()
        return r.json()

    def probe_write_permission(self, repo: str) -> tuple[bool, str]:
        """探测机器人身份对该仓库是否具备 **issue 写权限**。

        注意：`GET /repos/{repo}` 的 `permissions.push` 反映的是 Contents 写权限，
        对只申请 `Issues: write` 的 GitHub App 会恒为 false（误报）。因此：
        - app 模式：读 installation 实际授予的 `permissions.issues`（可靠来源）；
        - pat 模式：退回原判据（PAT 没有 installation 详情可查）。
        """
        if self._installation_permissions is not None:
            perms = self._installation_permissions() or {}
            issues = str(perms.get("issues", "")).lower()
            if issues:
                return issues == "write", f"{repo} installation.permissions.issues={issues}"
        data = self.get_repo(repo)
        perms = data.get("permissions") or {}
        push = bool(perms.get("push"))
        return push, f"{repo} permissions.push={push}"

    # ---------- issue 只读 ----------
    def list_issues(
        self,
        repo: str,
        state: str = "open",
        since: str | None = None,
        max_items: int = _MAX_ISSUES_PER_REPO,
    ) -> list[dict]:
        """分页拉取 issue（自动跳过 PR——带 `pull_request` 字段的条目）。"""
        out: list[dict] = []
        page = 1
        while len(out) < max_items:
            params: dict[str, Any] = {
                "state": state,
                "per_page": 100,
                "page": page,
                "sort": "updated",
                "direction": "desc",
            }
            if since:
                params["since"] = since
            r = self._client.get(
                f"{self.api_base}/repos/{repo}/issues",
                headers=self._headers(),
                params=params,
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            for item in batch:
                if "pull_request" in item:
                    continue
                out.append(item)
                if len(out) >= max_items:
                    break
            if len(batch) < 100:
                break
            page += 1
        return out

    def get_issue(self, repo: str, number: int) -> dict:
        r = self._client.get(
            f"{self.api_base}/repos/{repo}/issues/{number}", headers=self._headers()
        )
        r.raise_for_status()
        return r.json()

    def list_recent_issues(
        self,
        repo: str,
        since: str,
        state: str = "open",
        max_items: int = _MAX_ISSUES_PER_REPO,
    ) -> list[dict]:
        """巡检用：拉取 `since`（ISO8601）之后有更新的 issue（已剔除 PR）。

        与 `list_issues` 是同一套逻辑（分页 + 跳过含 `pull_request` 的条目），
        此处只做 PLAN §6.3 命名的薄封装，不重复实现拉取逻辑。
        """
        return self.list_issues(repo, state=state, since=since, max_items=max_items)

    def _get_recent_page(self, url: str, params: dict[str, Any], accept: str) -> list[dict]:
        """取「最近」的一页（per_page=100）：先取第 1 页读 Link 里的 last，再取该页。

        评论 / timeline 都是按时间正序返回的，若只截前 N 条会丢掉最新动向；
        这里改成取最后一页（最多 100 条），既省请求又保证「最近」的信息不丢。
        """
        base = {"per_page": 100, "page": 1}
        base.update(params)
        r = self._client.get(url, headers=self._headers(accept), params=base)
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list):
            return []
        last = (r.links.get("last") or {}).get("url")
        if last and len(batch) >= int(base["per_page"]):
            r2 = self._client.get(last, headers=self._headers(accept))
            r2.raise_for_status()
            tail = r2.json()
            if isinstance(tail, list) and tail:
                return tail
        return batch

    def get_issue_comments(
        self, repo: str, number: int, max_items: int = _MAX_COMMENTS_PER_ISSUE
    ) -> list[dict]:
        """拉取 issue 评论（取最近 `max_items` 条；评论总数见 issue 对象的 `comments`）。"""
        items = self._get_recent_page(
            f"{self.api_base}/repos/{repo}/issues/{number}/comments", {}, "application/vnd.github+json"
        )
        return items[-max(1, int(max_items)):]

    def get_issue_timeline(
        self, repo: str, number: int, max_items: int = _MAX_TIMELINE_PER_ISSUE
    ) -> list[dict]:
        """拉取 issue timeline（progression 判据：cross-referenced / labeled / assigned / closed …）。"""
        items = self._get_recent_page(
            f"{self.api_base}/repos/{repo}/issues/{number}/timeline", {}, _TIMELINE_ACCEPT
        )
        return items[-max(1, int(max_items)):]

    def search_linked_prs(
        self, repo: str, number: int, max_items: int = _MAX_LINKED_PRS
    ) -> list[dict]:
        """兜底：timeline 里拿不到关联 PR 时，用搜索接口按编号找（PLAN §6.3）。"""
        r = self._client.get(
            f"{self.api_base}/search/issues",
            headers=self._headers(),
            params={"q": f"repo:{repo} is:pr {number}", "per_page": max(1, int(max_items))},
        )
        r.raise_for_status()
        items = (r.json() or {}).get("items") or []
        return [i for i in items if isinstance(i, dict)][: max(1, int(max_items))]

    # ---------- issue 写操作（M3：人工确认后由机器人身份回写）----------
    def post_comment(self, repo: str, number: int, body: str) -> dict:
        """在 issue 下发表评论（回写主通道）。返回评论对象（含 html_url）。"""
        r = self._client.post(
            f"{self.api_base}/repos/{repo}/issues/{number}/comments",
            headers=self._headers(),
            json={"body": body},
        )
        r.raise_for_status()
        return r.json()

    def add_labels(self, repo: str, number: int, labels: list[str]) -> list[dict]:
        """为 issue 添加标签（需真人勾选后调用）。"""
        r = self._client.post(
            f"{self.api_base}/repos/{repo}/issues/{number}/labels",
            headers=self._headers(),
            json={"labels": list(labels)},
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    def remove_label(self, repo: str, number: int, label: str) -> None:
        """移除 issue 的某个标签。"""
        r = self._client.delete(
            f"{self.api_base}/repos/{repo}/issues/{number}/labels/{quote(label, safe='')}",
            headers=self._headers(),
        )
        r.raise_for_status()

    def assign(self, repo: str, number: int, assignees: list[str]) -> dict:
        """指派 issue 处理人。"""
        r = self._client.post(
            f"{self.api_base}/repos/{repo}/issues/{number}/assignees",
            headers=self._headers(),
            json={"assignees": list(assignees)},
        )
        r.raise_for_status()
        return r.json()

    def close_issue(self, repo: str, number: int) -> dict:
        """关闭 issue。"""
        return self._set_state(repo, number, "closed")

    def reopen_issue(self, repo: str, number: int) -> dict:
        """重新打开 issue。"""
        return self._set_state(repo, number, "open")

    def _set_state(self, repo: str, number: int, state: str) -> dict:
        r = self._client.patch(
            f"{self.api_base}/repos/{repo}/issues/{number}",
            headers=self._headers(),
            json={"state": state},
        )
        r.raise_for_status()
        return r.json()

    # ---------- commit（知识库过期判定）----------
    def get_latest_commit_sha(self, repo: str, branch: str = "") -> str:
        """取仓库最新 commit 的 sha（用于判断知识库索引是否过期）。

        未指定 branch 时取默认分支最新提交；失败抛异常。
        """
        params: dict[str, Any] = {"per_page": 1}
        if branch:
            params["sha"] = branch
        r = self._client.get(
            f"{self.api_base}/repos/{repo}/commits", headers=self._headers(), params=params
        )
        r.raise_for_status()
        commits = r.json()
        if not isinstance(commits, list) or not commits:
            raise RuntimeError(f"{repo} 未返回任何 commit（branch={branch or '默认'}）")
        sha = (commits[0] or {}).get("sha", "")
        if not sha:
            raise RuntimeError(f"{repo} 最新 commit 响应缺少 sha 字段")
        return sha


def build_github_api() -> GitHubApi:
    """按 `GITHUB_AUTH_MODE` 构造客户端：app 走 App 认证，其余走 PAT。"""
    mode = (Config.github_auth_mode or "app").lower()
    if mode == "pat":
        token = Config.github_bot_token
        return GitHubApi(Config.github_api_base, lambda: token)
    auth = GitHubAppAuth(
        Config.github_app_id,
        Config.github_app_private_key_path,
        Config.github_app_installation_id,
        Config.github_api_base,
    )
    return GitHubApi(
        Config.github_api_base,
        auth.installation_token,
        installation_permissions=auth.installation_permissions,
    )
