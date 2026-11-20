"""SQLite 访问层：知识库配置、索引/缓存、备份历史、发布幂等与审计。

知识库配置表（`kb_config`）遵循：**首启把 .env 播种进来，之后以 DB 为准**
（INSERT OR IGNORE，重启不覆盖已有值）。本服务不内置 Agent/LLM，因此不再有
`llm_config` / `digest_config` / `issue_tasks` / `issue_drafts` 等表。
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from config import Config

SCHEMA = """
-- 仓库索引状态
CREATE TABLE IF NOT EXISTS kb_index (
  repo          TEXT PRIMARY KEY,
  status        TEXT NOT NULL,          -- fresh/indexing/stale/failed
  indexed_sha   TEXT,
  indexed_at    TEXT,
  last_check_at TEXT,
  last_error    TEXT
);

-- 问答缓存
CREATE TABLE IF NOT EXISTS kb_cache (
  key           TEXT PRIMARY KEY,       -- repo+question 哈希 + 索引 sha
  repo          TEXT NOT NULL,
  question_hash TEXT NOT NULL,
  answer_json   TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

-- 知识库配置（单行）
CREATE TABLE IF NOT EXISTS kb_config (
  id               INTEGER PRIMARY KEY CHECK (id = 1),
  repos            TEXT NOT NULL,       -- 逗号分隔的持有仓库
  refresh_enabled  INTEGER NOT NULL,
  refresh_schedule TEXT NOT NULL,       -- "HH:MM"；空串=不定时
  backup_enabled   INTEGER NOT NULL,
  backup_schedule  TEXT NOT NULL,       -- "HH:MM"；空串=不定时
  backup_keep      INTEGER NOT NULL,
  last_refresh     TEXT NOT NULL,       -- 最近一次刷新日期 YYYY-MM-DD
  last_backup      TEXT NOT NULL,       -- 最近一次备份日期 YYYY-MM-DD
  updated_at       TEXT NOT NULL
);

-- 备份历史
CREATE TABLE IF NOT EXISTS kb_backup (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  path         TEXT NOT NULL,
  size_bytes   INTEGER NOT NULL DEFAULT 0,
  manifest_sha TEXT,
  trigger      TEXT NOT NULL,           -- scheduled/manual
  status       TEXT NOT NULL,           -- ok/failed/skipped
  created_at   TEXT NOT NULL
);

-- 飞书事件去重
CREATE TABLE IF NOT EXISTS processed_events (
  event_id TEXT PRIMARY KEY,
  ts       TEXT NOT NULL
);

-- 审计（谁在群里让机器人做了什么）
CREATE TABLE IF NOT EXISTS audit_log (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  open_id   TEXT NOT NULL,
  chat_id   TEXT NOT NULL DEFAULT '',
  action    TEXT NOT NULL,
  target    TEXT NOT NULL DEFAULT '',
  result    TEXT NOT NULL DEFAULT '',
  at        TEXT NOT NULL
);

-- 外部（用户 Agent）发布幂等记录
CREATE TABLE IF NOT EXISTS external_publishes (
  idempotency_key TEXT PRIMARY KEY,
  repo            TEXT NOT NULL,
  number          INTEGER NOT NULL,
  comment_url     TEXT,
  applied_json    TEXT NOT NULL DEFAULT '[]',
  errors_json     TEXT NOT NULL DEFAULT '[]',
  requester       TEXT NOT NULL DEFAULT '',
  created_at      TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_schedule(value: str) -> str:
    """把 'off'/'none'/空 归一为 ''（不定时）；否则返回原值（HH:MM）。"""
    v = (value or "").strip()
    return "" if v.lower() in ("", "off", "none", "关闭") else v


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(SCHEMA)

    # ---------- 首启播种 ----------
    def seed_from_config(self, cipher=None) -> None:
        """把 .env 的初值播种进知识库配置表（已存在则不覆盖）。"""
        self._seed_kb_config()

    def _seed_kb_config(self) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO kb_config
                   (id, repos, refresh_enabled, refresh_schedule, backup_enabled,
                    backup_schedule, backup_keep, last_refresh, last_backup, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?, '', '', ?)""",
                (
                    ",".join(Config.github_repos),
                    1 if Config.kb_refresh_enabled else 0,
                    _normalize_schedule(Config.kb_refresh_schedule),
                    1 if Config.kb_backup_enabled else 0,
                    _normalize_schedule(Config.kb_backup_schedule),
                    max(1, Config.kb_backup_keep),
                    _now(),
                ),
            )

    # ---------- 事件去重 ----------
    def mark_event(self, event_id: str) -> bool:
        """返回 True 表示首次见到该事件（可继续处理）；False 表示重复。

        event_id 为空时不参与去重，视为首次。
        """
        if not event_id:
            return True
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO processed_events (event_id, ts) VALUES (?, ?)",
                (event_id, _now()),
            )
            return cur.rowcount == 1

    def cleanup_events(self, keep_days: int = 30) -> int:
        """清理超过 keep_days 的事件去重记录（按 ts 文本比较，ISO8601 可直接比）。"""
        cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM processed_events WHERE ts < ?", (cutoff_iso,))
            return cur.rowcount

    # ---------- kb_config（单行配置）----------
    def get_kb_config(self) -> sqlite3.Row:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM kb_config WHERE id=1").fetchone()
        if row:
            return row
        self._seed_kb_config()
        with self._conn() as conn:
            return conn.execute("SELECT * FROM kb_config WHERE id=1").fetchone()

    def update_kb_config(self, **fields) -> None:
        """按需更新 repos/refresh_enabled/refresh_schedule/backup_enabled/backup_schedule/backup_keep。"""
        allowed = {
            "repos",
            "refresh_enabled",
            "refresh_schedule",
            "backup_enabled",
            "backup_schedule",
            "backup_keep",
        }
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return
        if isinstance(updates.get("repos"), (list, tuple)):
            updates["repos"] = ",".join(updates["repos"])
        for flag in ("refresh_enabled", "backup_enabled"):
            if flag in updates:
                updates[flag] = 1 if updates[flag] else 0
        for sched in ("refresh_schedule", "backup_schedule"):
            if sched in updates:
                updates[sched] = _normalize_schedule(updates[sched])
        if "backup_keep" in updates:
            updates["backup_keep"] = max(1, int(updates["backup_keep"]))
        cols = ", ".join(f"{k}=?" for k in updates)
        with self._lock, self._conn() as conn:
            conn.execute(
                f"UPDATE kb_config SET {cols}, updated_at=? WHERE id=1",
                (*updates.values(), _now()),
            )

    def mark_kb_refresh_run(self, date_str: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE kb_config SET last_refresh=?, updated_at=? WHERE id=1",
                (date_str, _now()),
            )

    def mark_kb_backup_run(self, date_str: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE kb_config SET last_backup=?, updated_at=? WHERE id=1",
                (date_str, _now()),
            )

    # ---------- kb_index ----------
    def get_kb_index(self, repo: str) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM kb_index WHERE repo=?", (repo,)).fetchone()

    def upsert_kb_index(self, repo: str, status: str, indexed_sha: str | None = None) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO kb_index (repo, status, indexed_sha, indexed_at, last_check_at, last_error)
                   VALUES (?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(repo) DO UPDATE SET
                     status=excluded.status,
                     indexed_sha=COALESCE(excluded.indexed_sha, kb_index.indexed_sha),
                     indexed_at=COALESCE(excluded.indexed_at, kb_index.indexed_at),
                     last_check_at=excluded.last_check_at""",
                (repo, status, indexed_sha, _now() if indexed_sha else None, _now()),
            )

    # ---------- external_publishes（外部发布幂等）----------
    def get_external_publish(self, idempotency_key: str) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM external_publishes WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()

    def record_external_publish(
        self,
        idempotency_key: str,
        repo: str,
        number: int,
        comment_url: str,
        applied_json: str,
        errors_json: str,
        requester: str,
    ) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO external_publishes
                   (idempotency_key, repo, number, comment_url, applied_json, errors_json, requester, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    idempotency_key,
                    repo,
                    int(number),
                    comment_url,
                    applied_json,
                    errors_json,
                    requester,
                    _now(),
                ),
            )

    # ---------- audit_log ----------
    def write_audit(
        self,
        open_id: str,
        action: str,
        chat_id: str = "",
        target: str = "",
        result: str = "",
    ) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO audit_log (open_id, chat_id, action, target, result, at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (open_id, chat_id, action, target, result, _now()),
            )

    # ---------- kb_backup ----------
    def record_kb_backup(
        self, path: str, trigger: str, status: str, size_bytes: int = 0, manifest_sha: str = ""
    ) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO kb_backup (path, size_bytes, manifest_sha, trigger, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (path, size_bytes, manifest_sha, trigger, status, _now()),
            )
            return int(cur.lastrowid)

    def list_kb_backups(self, limit: int = 20) -> list[sqlite3.Row]:
        """按时间倒序列出最近若干条备份记录。"""
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM kb_backup ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()

    def get_last_kb_backup(self) -> Optional[sqlite3.Row]:
        """取最近一条备份记录（含失败的），无则返回 None。"""
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM kb_backup ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()

    def delete_kb_backup(self, backup_id: int) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM kb_backup WHERE id=?", (int(backup_id),))

    def prune_kb_backup_rows(self, keep: int) -> list[str]:
        """按 created_at 倒序保留最近 keep 条，删除更旧的 DB 行；返回被删行的归档路径列表（供上层删文件）。"""
        keep = max(1, int(keep))
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT id, path FROM kb_backup ORDER BY created_at DESC, id DESC"
            ).fetchall()
            stale = rows[keep:]
            for row in stale:
                conn.execute("DELETE FROM kb_backup WHERE id=?", (row["id"],))
            return [row["path"] for row in stale]

    # ---------- kb_cache（问答缓存）----------
    def get_kb_cache(self, key: str) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM kb_cache WHERE key=?", (key,)).fetchone()

    def put_kb_cache(self, key: str, repo: str, question_hash: str, answer_json: str) -> None:
        """写入/更新一条问答缓存（UPSERT）。"""
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO kb_cache (key, repo, question_hash, answer_json, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     answer_json=excluded.answer_json,
                     created_at=excluded.created_at""",
                (key, repo, question_hash, answer_json, _now()),
            )

    def invalidate_kb_cache(self, repo: str) -> int:
        """失效某仓库的全部问答缓存，返回删除条数。"""
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM kb_cache WHERE repo=?", (repo,))
            return cur.rowcount

    # ---------- kb_index（补充）----------
    def list_kb_index(self) -> list[sqlite3.Row]:
        """列出全部仓库索引状态（按仓库名排序）。"""
        with self._conn() as conn:
            return conn.execute("SELECT * FROM kb_index ORDER BY repo").fetchall()

    def mark_kb_index_check(
        self,
        repo: str,
        status: str | None = None,
        indexed_sha: str | None = None,
        last_error: str | None = None,
    ) -> None:
        """记录一次索引状态检查：总能刷新 last_check_at。

        - 传 status 则更新状态；status 为 fresh/indexed 时清空 last_error；
        - 传 indexed_sha 则同时刷新 indexed_at；
        - 传 last_error（如 status=failed）则写入错误原因；
        - 行不存在时按给定值插入。
        """
        now = _now()
        fields: dict[str, Any] = {}
        if status:
            fields["status"] = status
        if indexed_sha is not None:
            fields["indexed_sha"] = indexed_sha
            fields["indexed_at"] = now
        if last_error is not None:
            fields["last_error"] = last_error
        elif status in ("fresh", "indexed"):
            fields["last_error"] = None
        fields["last_check_at"] = now
        with self._lock, self._conn() as conn:
            exists = conn.execute("SELECT repo FROM kb_index WHERE repo=?", (repo,)).fetchone()
            if not exists:
                conn.execute(
                    """INSERT INTO kb_index (repo, status, indexed_sha, indexed_at, last_check_at, last_error)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        repo,
                        status or "unknown",
                        indexed_sha,
                        now if indexed_sha else None,
                        now,
                        last_error,
                    ),
                )
                return
            cols = ", ".join(f"{k}=?" for k in fields)
            conn.execute(
                f"UPDATE kb_index SET {cols} WHERE repo=?",
                (*fields.values(), repo),
            )
