"""platform-agent-adaptor：平台 Agent 适配服务（HTTP only，不包含飞书接入）。

能力：
- 外部 Agent 的知识库只读检索 / wiki；
- 外部 Agent 草稿经人工确认后的发布（可选仅执行动作）；
- 保留内部 LLM 草稿生成、巡检与知识库刷新/备份引擎，供后续平台能力复用。
"""
from __future__ import annotations

import logging
import queue
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from adapters.deepwiki_client import build_deepwiki_client
from adapters.github_api import build_github_api
from agents import get_backend, list_backends
from app.http_api import create_http_server, start_in_thread
from config import Config
from engine.issue_digest import IssueDigestEngine
from engine.issue_handle import IssueHandleEngine
from engine.kb import KbEngine
from knowledge import get_knowledge_base
from scripts import backup_kb
from store.crypto import TokenCipher
from store.db import Database

logger = logging.getLogger("agent_adaptor")


class AgentAdaptorService:
    def __init__(self):
        self.db = Database(Config.db_path)
        self.cipher = TokenCipher(Config.fernet_key)
        self.db.seed_from_config(self.cipher)

        self.backend = get_backend()
        self.github = build_github_api()
        self.kb = build_deepwiki_client()
        self.kb_engine = KbEngine(self.db, self.github, get_knowledge_base())
        self.digest = IssueDigestEngine(self.github, self.backend)
        self.handle = IssueHandleEngine(self.db, self.github, self.backend, self.kb_engine)

        self.tasks: "queue.Queue[tuple[str, dict[str, Any]]]" = queue.Queue()
        self._stop = threading.Event()
        self._platform_errors: dict[int, str] = {}
        self._server = None

    # ---------------- 生命周期 ----------------
    def start_background(self) -> None:
        for index in range(max(1, Config.queue_workers)):
            threading.Thread(target=self._worker, name=f"worker-{index}", daemon=True).start()
        threading.Thread(target=self._scheduler, name="kb-scheduler", daemon=True).start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001 - 退出阶段只记日志
                logger.exception("关闭 HTTP API 失败")

    # ---------------- 仓库白名单 ----------------
    def is_supported_repo(self, repo: str) -> bool:
        target = (repo or "").strip().lower()
        if not target:
            return False
        allowed = {item.strip().lower() for item in Config.github_repos if item.strip()}
        allowed |= {item.strip().lower() for item in self._digest_repos() if item.strip()}
        return target in allowed

    def _digest_repos(self) -> list[str]:
        config = self.db.get_digest_config()
        return [item.strip() for item in (config["repos"] or "").split(",") if item.strip()]

    # ---------------- 内部 LLM 草稿生成 ----------------
    def start_from_platform(
        self, repo: str, number: int, requester: str = "platform", hint: str = ""
    ) -> int:
        repo = (repo or "").strip()
        number = int(number)
        if number < 1:
            raise ValueError("issue 编号不合法")
        if not self.is_supported_repo(repo):
            raise ValueError(f"仓库未托管：{repo}")
        hint = (hint or "").strip()[:500]
        requester = (requester or "platform").strip()[:120] or "platform"
        task_id = self.handle.start(repo, number, requester, hint)
        self.tasks.put(
            (
                "issue_handle_platform",
                {"task_id": task_id, "repo": repo, "number": number, "hint": hint},
            )
        )
        self.db.write_audit(
            requester, "issue.handle.platform", chat_id="", target=f"{repo}#{number}"
        )
        return task_id

    def platform_task_status(self, repo: str, number: int) -> dict[str, Any]:
        task = self.db.get_latest_issue_task(repo, int(number))
        if task is None:
            return {"task": None, "draft": None, "feishuNotifyReady": False}
        task_id = int(task["id"])
        draft = self.db.get_latest_issue_draft(task_id)
        payload: dict[str, Any] = {
            "task": {
                "id": task_id,
                "status": task["status"],
                "createdAt": task["created_at"],
                "requester": task["requester_open_id"],
            },
            "draft": None
            if draft is None
            else {
                "status": draft["status"],
                "commentUrl": draft["comment_url"] or "",
                "updatedAt": draft["updated_at"],
            },
            # 适配器不包含飞书通知渠道；平台据此提示草稿无法在飞书确认。
            "feishuNotifyReady": False,
        }
        error = self._platform_errors.get(task_id)
        if error:
            payload["error"] = error
        return payload

    # ---------------- 知识库只读 ----------------
    def kb_search(self, repo: str, question: str, top_k: int = 8) -> dict[str, Any]:
        if not self.is_supported_repo(repo):
            raise ValueError(f"仓库未托管：{repo}")
        return self.kb_engine.search("platform", repo, question, top_k=int(top_k))

    def kb_wiki(self, repo: str, topic: str) -> dict[str, Any]:
        if not self.is_supported_repo(repo):
            raise ValueError(f"仓库未托管：{repo}")
        return {"repo": repo, "topic": topic, "text": self.kb_engine.wiki(repo, topic)}

    # ---------------- 外部草稿发布 ----------------
    def publish_external(
        self,
        repo: str,
        number: int,
        comment_md: str,
        actions: list | None,
        requester: str = "",
        idempotency_key: str = "",
        post_comment: bool = True,
    ) -> dict[str, Any]:
        if not self.is_supported_repo(repo):
            raise ValueError(f"仓库未托管：{repo}")
        result = self.handle.publish_external(
            repo,
            int(number),
            comment_md,
            actions=actions,
            requester=requester,
            idempotency_key=idempotency_key,
            post_comment=post_comment,
        )
        return {
            "commentUrl": result.comment_url,
            "applied": result.applied,
            "errors": result.errors,
            "replayed": result.replayed,
        }

    # ---------------- 任务队列 ----------------
    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                kind, payload = self.tasks.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._dispatch(kind, payload)
            except Exception as exc:  # noqa: BLE001 - 单任务失败不拖垮 worker
                logger.error("任务 %s 执行失败：%s", kind, exc)
            finally:
                self.tasks.task_done()

    def _dispatch(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "issue_handle_platform":
            self._dispatch_platform_handle(payload)
        else:
            logger.warning("未知任务类型：%s", kind)

    def _dispatch_platform_handle(self, payload: dict[str, Any]) -> None:
        task_id = int(payload.get("task_id") or 0)
        try:
            view = self.handle.generate(task_id)
        except Exception as exc:  # noqa: BLE001 - 失败必须如实记录
            logger.error("平台触发的处理草稿生成失败（task=%s）：%s", task_id, exc)
            self._platform_errors[task_id] = str(exc)
            try:
                self.db.update_issue_task_status(task_id, "failed")
            except Exception:  # noqa: BLE001
                logger.exception("平台任务状态落库失败（task=%s）", task_id)
            return
        self._platform_errors.pop(task_id, None)
        logger.info(
            "平台触发的处理草稿已生成：%s#%s（draft=%s，%d 个拟执行动作）",
            view.repo, view.number, view.draft_id, len(view.actions),
        )

    # ---------------- 知识库定时刷新 / 备份（无飞书通知，仅日志） ----------------
    def _scheduler(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.error("定时检查出错：%s", exc)
            self._stop.wait(30)

    def _tick(self) -> None:
        now = datetime.now()
        hhmm = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")
        config = self.db.get_kb_config()
        if config["refresh_enabled"] and config["refresh_schedule"] == hhmm and config["last_refresh"] != today:
            try:
                results = self.kb_engine.refresh(trigger="scheduled")
                self.db.mark_kb_refresh_run(today)
                logger.info("知识库定时刷新完成：%s", self._format_refresh(results))
            except Exception as exc:  # noqa: BLE001
                logger.error("知识库定时刷新失败：%s", exc)
        if config["backup_enabled"] and config["backup_schedule"] == hhmm and config["last_backup"] != today:
            try:
                result = backup_kb.create_backup(trigger="scheduled", db=self.db)
                backup_kb.prune_backups(keep=config["backup_keep"], db=self.db)
                self.db.mark_kb_backup_run(today)
                logger.info("知识库定时备份：%s — %s", result["status"], result["detail"])
            except Exception as exc:  # noqa: BLE001
                logger.error("知识库定时备份失败：%s", exc)

    @staticmethod
    def _format_refresh(results: list[dict]) -> str:
        return "；".join(
            f"{item['repo']}={item['status']}" + (f"({item.get('detail')})" if item.get("detail") else "")
            for item in results
        )


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _startup_selfcheck(service: AgentAdaptorService) -> None:
    logger.info("启动自检：")

    def check(name: str, fn) -> None:
        try:
            detail = fn()
            logger.info("  [OK] %s%s", name, f"（{detail}）" if detail else "")
        except Exception as exc:  # noqa: BLE001 - 自检不阻断启动
            logger.warning("  [FAIL] %s：%s", name, exc)

    def llm() -> str:
        config = service.db.get_llm_config()
        has_key = bool(config["api_key_enc"])
        detail = service.backend.probe()
        return (
            f"base_url={config['base_url']}, model={config['model']}, "
            f"api_key={'已配置' if has_key else '未配置'}, backend={service.backend.name}"
            + (f", {detail}" if detail else "")
        )

    def github() -> str:
        repo = Config.github_default_repo
        ok, detail = service.github.probe_write_permission(repo)
        if not ok:
            raise RuntimeError(f"无写权限：{detail}")
        return detail

    def kb() -> str:
        detail = service.kb.probe()
        states = service.kb_engine.status()
        if not states:
            return f"{detail}；索引：无记录"
        summary = "，".join(
            f"{state['repo']}={state['status']}" + (f"@{state['indexed_sha'][:8]}" if state["indexed_sha"] else "")
            for state in states
        )
        return f"{detail}；索引：{summary}"

    def backup_dir() -> str:
        path = Config.kb_backup_dir
        Path(path).mkdir(parents=True, exist_ok=True)
        probe = Path(path) / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return f"{path} 可写"

    check("LLM 配置", llm)
    check("GitHub API（连通 + 写权限）", github)
    check("知识库 KB_BASE_URL", kb)
    check("备份目录可写性", backup_dir)
    check("平台接入 API", lambda: f"{Config.api_host}:{Config.api_port}")
    logger.info(
        "配置回显：GITHUB_AUTH_MODE=%s, GITHUB_REPOS=%s, AGENT_BACKEND=%s, "
        "已注册后端=%s, KB_BASE_URL=%s, DB_PATH=%s",
        Config.github_auth_mode,
        ",".join(Config.github_repos) or "（空）",
        Config.agent_backend,
        ",".join(list_backends()),
        Config.kb_base_url,
        Config.db_path,
    )


def _start_api(service: AgentAdaptorService) -> None:
    if not Config.api_token:
        raise RuntimeError("缺少 ADAPTOR_API_TOKEN（或 ISSUE_MASTER_API_TOKEN）")
    server = create_http_server(
        service,
        host=Config.api_host,
        port=Config.api_port,
        token=Config.api_token,
        repo_allowed=service.is_supported_repo,
        rate_limit_per_minute=Config.api_rate_limit,
        tls_cert=Config.api_tls_cert,
        tls_key=Config.api_tls_key,
    )
    start_in_thread(server)
    service._server = server
    logger.info(
        "平台接入 API 已启动：%s://%s:%s",
        "https" if Config.api_tls_cert else "http",
        Config.api_host,
        Config.api_port,
    )


def main() -> None:
    _setup_logging()
    missing = Config.validate()
    if missing:
        logger.error("缺少必要配置：%s", ", ".join(missing))
        logger.error("请参考 .env.example 配置 .env 后重试。")
        sys.exit(1)
    if not Config.api_enabled:
        logger.error("ADAPTOR_API_ENABLED 未开启；本服务只提供 HTTP 接入。")
        sys.exit(1)

    service = AgentAdaptorService()
    logger.info("已选用 Agent 后端：%s", service.backend.name)
    _startup_selfcheck(service)
    service.start_background()
    try:
        _start_api(service)
    except Exception as exc:  # noqa: BLE001
        logger.error("平台接入 API 启动失败：%s", exc)
        service.shutdown()
        sys.exit(1)

    stop = threading.Event()

    def handle_signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    logger.info("platform-agent-adaptor 已启动（HTTP API，不含飞书）")
    stop.wait()
    service.shutdown()
    logger.info("platform-agent-adaptor 已退出")


if __name__ == "__main__":
    main()
