"""platform-agent-adaptor：平台 Agent 适配服务（HTTP only，不包含飞书）。

能力：
- 外部 Agent 的知识库只读检索 / wiki；
- 外部 Agent 草稿经人工确认后的发布（可选仅执行动作）；
- 知识库刷新 / 备份引擎。

本服务不内置 Agent 与 LLM：处理草稿完全由用户自己的 Agent 生成。
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from adapters.deepwiki_client import build_deepwiki_client
from adapters.github_api import build_github_api
from app.http_api import create_http_server, start_in_thread
from config import Config
from engine.kb import KbEngine
from engine.publish import ExternalPublishEngine
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

        self.github = build_github_api()
        self.kb = build_deepwiki_client()
        self.kb_engine = KbEngine(self.db, self.github, get_knowledge_base())
        self.publish = ExternalPublishEngine(self.db, self.github)

        self._stop = threading.Event()
        self._server = None

    # ---------------- 生命周期 ----------------
    def start_background(self) -> None:
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
        allowed |= {item.strip().lower() for item in self.kb_engine.repos() if item.strip()}
        return target in allowed

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
        result = self.publish.publish_external(
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

    check("GitHub API（连通 + 写权限）", github)
    check("知识库 KB_BASE_URL", kb)
    check("备份目录可写性", backup_dir)
    check("平台接入 API", lambda: f"{Config.api_host}:{Config.api_port}")
    logger.info(
        "配置回显：GITHUB_AUTH_MODE=%s, GITHUB_REPOS=%s, KB_BASE_URL=%s, DB_PATH=%s",
        Config.github_auth_mode,
        ",".join(Config.github_repos) or "（空）",
        Config.kb_base_url,
        Config.db_path,
    )


def _start_api(service: AgentAdaptorService) -> None:
    if not Config.api_token:
        raise RuntimeError("缺少 ADAPTOR_API_TOKEN")
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
    logger.info("platform-agent-adaptor 已启动（HTTP API，不含飞书与内置 LLM）")
    stop.wait()
    service.shutdown()
    logger.info("platform-agent-adaptor 已退出")


if __name__ == "__main__":
    main()
