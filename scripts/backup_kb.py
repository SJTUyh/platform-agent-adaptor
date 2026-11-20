"""知识库备份 / 还原脚本（`scripts/backup_kb.py`）。

备份内容（PLAN §5.5）：
1. deepwiki-open 的数据卷（向量库 + 已生成 wiki），路径 = `KB_DATA_DIR`；
2. 本项目 DB 的 `kb_index` / `kb_cache` 两张表。

方式：先做一致性快照（数据卷拷到临时目录、DB 单次事务读取）→ 打包 `tar.gz` 到
`KB_BACKUP_DIR`（文件名带时间戳）→ 归档内附 `manifest.json`（各仓库 `indexed_sha`、
文件数、逐文件 sha256）。滚动保留最近 `KB_BACKUP_KEEP` 份，最旧的自动清理。

命令行：
    python scripts/backup_kb.py --create           # 立即备份
    python scripts/backup_kb.py --list             # 列出备份历史
    python scripts/backup_kb.py --restore <归档>    # 还原（停服后执行，需确认）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# 允许以 `python scripts/backup_kb.py` 直接运行（把仓库根目录加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config  # noqa: E402
from store.db import Database  # noqa: E402

MANIFEST_NAME = "manifest.json"
DB_TABLE_DUMP = "db_tables.json"
_KB_TABLES = ("kb_index", "kb_cache")


def _now_compact() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _dump_db_tables(db_path: str) -> dict:
    """单次事务读取 kb_index / kb_cache 全部行，返回可 JSON 化的结构。"""
    out: dict[str, list[dict]] = {t: [] for t in _KB_TABLES}
    if not Path(db_path).exists():
        return out
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        for table in _KB_TABLES:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            out[table] = [dict(r) for r in rows]
    finally:
        conn.close()
    return out


def _count_files(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(1 for p in root.rglob("*") if p.is_file())


def create_backup(trigger: str = "manual", db: Database | None = None) -> dict:
    """执行一次备份，返回 {status, path, size_bytes, manifest_sha, detail}。

    status：ok / skipped（磁盘余量不足）/ failed。
    """
    owns_db = db is None
    db = db or Database(Config.db_path)

    backup_dir = Path(Config.kb_backup_dir)
    data_dir = Path(Config.kb_data_dir)

    # 磁盘余量检查（低于阈值 → skipped，不静默当成功）
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(backup_dir).free / (1024 ** 3)
        if free_gb < Config.kb_backup_min_free_gb:
            detail = (
                f"磁盘剩余 {free_gb:.1f}GB 低于阈值 {Config.kb_backup_min_free_gb}GB，跳过备份"
            )
            db.record_kb_backup("", trigger, "skipped")
            return {"status": "skipped", "path": "", "size_bytes": 0, "manifest_sha": "", "detail": detail}
    except Exception as exc:  # noqa: BLE001 - 目录不可用必须回报，不假装成功
        msg = f"备份目录不可用：{exc}"
        db.record_kb_backup("", trigger, "failed")
        return {"status": "failed", "path": "", "size_bytes": 0, "manifest_sha": "", "detail": msg}

    staging = Path(tempfile.mkdtemp(prefix="kb_backup_"))
    try:
        # 1. 快照数据卷（拷贝到临时目录，避免打包中间态）
        #    数据卷缺失或为空 → 没有任何内容可备，必须回报 failed；
        #    否则会产出一个空壳归档却报 ok（KB_DATA_DIR 指错时就会这样）。
        reason = ""
        if not data_dir.exists():
            reason = "不存在"
        elif not any(p.is_file() for p in data_dir.rglob("*")):
            reason = "为空（无任何文件）"
        if reason:
            msg = (
                f"数据卷{reason}：{data_dir}，未执行备份"
                "（请检查 KB_DATA_DIR 是否指向 deepwiki 数据目录）"
            )
            db.record_kb_backup("", trigger, "failed")
            return {"status": "failed", "path": "", "size_bytes": 0, "manifest_sha": "", "detail": msg}
        shutil.copytree(data_dir, staging / "deepwiki_open", dirs_exist_ok=True)

        # 2. 导出 DB 两张表
        tables = _dump_db_tables(Config.db_path)
        (staging / DB_TABLE_DUMP).write_text(
            json.dumps(tables, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 3. manifest：仓库 sha / 文件数 / 逐文件校验和
        files: dict[str, str] = {}
        for p in sorted(staging.rglob("*")):
            if p.is_file():
                files[str(p.relative_to(staging)).replace("\\", "/")] = _sha256_file(p)
        repos = [
            {
                "repo": r["repo"],
                "status": r["status"],
                "indexed_sha": r["indexed_sha"] or "",
                "indexed_at": r["indexed_at"] or "",
            }
            for r in tables.get("kb_index", [])
        ]
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "trigger": trigger,
            "kb_data_dir": str(data_dir),
            "kb_data_dir_exists": True,
            "repos": repos,
            "counts": {
                "files": len(files),
                "kb_index": len(tables.get("kb_index", [])),
                "kb_cache": len(tables.get("kb_cache", [])),
            },
            "checksums": files,
        }
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        (staging / MANIFEST_NAME).write_bytes(manifest_bytes)
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

        # 4. 打包
        archive = backup_dir / f"kb_backup_{_now_compact()}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            # 只添加非目录条目：tar.add() 传目录会递归打包整棵子树，而这里已用
            # rglob("*") 逐个遍历，两者叠加会让每个文件按「自身 + 每层祖先目录」
            # 重复写入（实测归档会胀大 4~5 倍）。文件条目的父目录由解压端自动补建。
            for p in sorted(staging.rglob("*")):
                if p.is_dir():
                    continue
                tar.add(p, arcname=str(p.relative_to(staging)).replace("\\", "/"))
        size = archive.stat().st_size

        db.record_kb_backup(str(archive), trigger, "ok", size_bytes=size, manifest_sha=manifest_sha)
        return {
            "status": "ok",
            "path": str(archive),
            "size_bytes": size,
            "manifest_sha": manifest_sha,
            "detail": (
                f"已备份 {manifest['counts']['files']} 个文件，"
                f"kb_index {manifest['counts']['kb_index']} 行、kb_cache {manifest['counts']['kb_cache']} 行"
            ),
        }
    except Exception as exc:  # noqa: BLE001 - 失败必须回报
        msg = f"备份失败：{exc}"
        db.record_kb_backup("", trigger, "failed")
        return {"status": "failed", "path": "", "size_bytes": 0, "manifest_sha": "", "detail": msg}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if owns_db:
            pass  # Database 无长连接，无需关闭


def prune_backups(keep: int | None = None, db: Database | None = None) -> list[str]:
    """滚动清理：保留最近 N 份，删除更旧的归档文件与 DB 行；返回被删除的归档路径。"""
    db = db or Database(Config.db_path)
    keep = max(1, int(keep if keep is not None else Config.kb_backup_keep))
    removed_paths = db.prune_kb_backup_rows(keep)
    for path in removed_paths:
        try:
            p = Path(path)
            if p.exists():
                p.unlink()
        except OSError as exc:  # noqa: BLE001 - 文件删除失败只告警，保留 DB 行已删的既成事实
            print(f"[warn] 删除归档失败：{path}（{exc}）")
    return removed_paths


def list_backups(limit: int = 20, db: Database | None = None) -> list[dict]:
    """列出最近的备份记录（倒序）。"""
    db = db or Database(Config.db_path)
    return [dict(r) for r in db.list_kb_backups(limit)]


def restore_backup(archive_path: str, assume_yes: bool = False, db: Database | None = None) -> str:
    """从归档还原数据卷与 DB 两张表。停服后执行；未 --yes 时需交互确认。

    返回一句人类可读的结果描述；失败抛异常。
    """
    archive = Path(archive_path)
    if not archive.exists():
        raise FileNotFoundError(f"归档不存在：{archive}")

    if not assume_yes:
        prev = db or Database(Config.db_path)
        existing = prev.get_last_kb_backup()
        ans = input(
            "⚠️ 还原会覆盖当前知识库数据卷与 kb_index/kb_cache 表。\n"
            f"   归档：{archive}\n"
            f"   目标数据卷：{Config.kb_data_dir}\n"
            f"   （最近一次备份：{existing['path'] if existing else '无'}）\n"
            "   请确认服务已停止。输入 yes 继续："
        )
        if ans.strip().lower() != "yes":
            return "已取消还原。"

    db = db or Database(Config.db_path)
    extract_dir = Path(tempfile.mkdtemp(prefix="kb_restore_"))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            _safe_extract(tar, extract_dir)

        manifest_file = extract_dir / MANIFEST_NAME
        manifest = json.loads(manifest_file.read_text(encoding="utf-8")) if manifest_file.exists() else {}

        # 1. 还原数据卷：先把现有数据卷挪到 .bak，再拷回
        data_dir = Path(Config.kb_data_dir)
        src_data = extract_dir / "deepwiki_open"
        if src_data.exists():
            if data_dir.exists():
                bak = data_dir.with_name(data_dir.name + f".bak_{_now_compact()}")
                shutil.move(str(data_dir), str(bak))
            data_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_data, data_dir)

        # 2. 还原 DB 两张表（整体替换）
        tables_file = extract_dir / DB_TABLE_DUMP
        restored_rows = 0
        if tables_file.exists():
            tables = json.loads(tables_file.read_text(encoding="utf-8"))
            restored_rows = _restore_db_tables(Config.db_path, tables)

        return (
            f"还原完成：归档={archive.name}，数据卷={'已还原' if src_data.exists() else '归档中未包含'}，"
            f"DB 行数={restored_rows}，manifest_sha={'有' if manifest else '无'}"
        )
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """防止归档内路径穿越（../）写到目标目录之外。"""
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest)):
            raise RuntimeError(f"归档包含非法路径，已中止：{member.name}")
    tar.extractall(dest)


def _restore_db_tables(db_path: str, tables: dict) -> int:
    """把 dump 的两张表整体写回 DB（先清空再插入），返回写入行数。"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        total = 0
        for table in _KB_TABLES:
            rows = tables.get(table) or []
            conn.execute(f"DELETE FROM {table}")
            if not rows:
                continue
            cols = list(rows[0].keys())
            placeholders = ", ".join("?" for _ in cols)
            col_sql = ", ".join(cols)
            conn.executemany(
                f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",
                [tuple(r.get(c) for c in cols) for r in rows],
            )
            total += len(rows)
        conn.commit()
        return total
    finally:
        conn.close()


# ---------------- CLI ----------------
def _cmd_create(args: argparse.Namespace) -> int:
    result = create_backup(trigger=args.trigger)
    print(f"[{result['status']}] {result['detail']}")
    if result["path"]:
        print(f"归档：{result['path']}（{result['size_bytes']} bytes，manifest_sha={result['manifest_sha'][:12]}…）")
    removed = prune_backups(db=None)
    if removed:
        print(f"滚动清理：删除 {len(removed)} 份旧归档")
    return 0 if result["status"] == "ok" else 1


def _cmd_list(args: argparse.Namespace) -> int:
    rows = list_backups(limit=args.limit)
    if not rows:
        print("（暂无备份记录）")
        return 0
    for r in rows:
        size_mb = r["size_bytes"] / (1024 * 1024)
        print(
            f"#{r['id']:<4} {r['created_at']}  [{r['status']}/{r['trigger']}]  "
            f"{size_mb:.1f}MB  {r['path'] or '(无归档)'}"
        )
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    print(restore_backup(args.restore, assume_yes=args.yes))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="知识库备份 / 还原（deepwiki-open 数据卷 + kb_index/kb_cache）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", action="store_true", help="立即执行一次备份")
    group.add_argument("--list", action="store_true", help="列出备份历史")
    group.add_argument("--restore", metavar="归档", help="从指定归档还原（停服后执行）")
    parser.add_argument("--trigger", default="manual", help="备份触发来源（manual/scheduled），仅 --create 用")
    parser.add_argument("--limit", type=int, default=20, help="--list 显示条数")
    parser.add_argument("--yes", action="store_true", help="--restore 跳过交互确认")
    args = parser.parse_args(argv)

    if args.create:
        return _cmd_create(args)
    if args.list:
        return _cmd_list(args)
    return _cmd_restore(args)


if __name__ == "__main__":
    raise SystemExit(main())
