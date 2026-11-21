r"""备份命令：把日志（含每个被代理服务）与数据库复制到备份目录。

用法：

    python -m gateway.backup                 # 备份到 GATEWAY_BACKUP_DIR
    python -m gateway.backup --keep 7        # 只保留最近 7 个快照
    python -m gateway.backup --dry-run       # 只打印将要做什么
    python -m gateway.backup prune-audit --days 180 --dry-run

目录结构（每个快照一个目录，日志按原类别/服务保留）：

    D:\Document\backup\logs\2026-11-21_120000\
    ├── manifest.json
    ├── database\gateway.sqlite3
    └── logs\
        ├── access\2026-11-21.log
        ├── audit\2026-11-21.log
        └── services\finance\2026-11-21.log
"""

import argparse
import json
import logging
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from .audit import AuditTrail
from .config import Settings
from .logging_setup import LogSettings, setup_logging
from .maintenance import prune_audit
from .storage import Store

logger = logging.getLogger(__name__)
SNAPSHOT_FORMAT = "%Y-%m-%d_%H%M%S"
SNAPSHOT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}(-\d+)?$")


def _snapshot_name(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime(SNAPSHOT_FORMAT)


def _copy_database(source: Path, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    origin = sqlite3.connect(source)
    try:
        destination = sqlite3.connect(target)
        try:
            origin.backup(destination)
        finally:
            destination.close()
    finally:
        origin.close()
    return target.stat().st_size


def _copy_logs(log_dir: Path, target: Path) -> list[dict]:
    if not log_dir.exists():
        return []
    shutil.copytree(log_dir, target, dirs_exist_ok=True)
    files = []
    for path in sorted(target.rglob("*")):
        if path.is_file():
            files.append({"path": path.relative_to(target).as_posix(),
                          "bytes": path.stat().st_size})
    return files


def _group_by_service(files: list[dict]) -> dict[str, dict]:
    """manifest 里按类别/服务汇总，便于确认“每个服务的日志都备到了”。"""
    groups: dict[str, dict] = {}
    for item in files:
        parts = item["path"].split("/")
        name = parts[1] if parts[0] == "services" and len(parts) > 1 else parts[0]
        group = groups.setdefault(name, {"files": 0, "bytes": 0})
        group["files"] += 1
        group["bytes"] += item["bytes"]
    return groups


def _prune_snapshots(dest: Path, keep: int) -> list[str]:
    if keep <= 0:
        return []
    snapshots = sorted(
        path for path in dest.iterdir()
        if path.is_dir() and SNAPSHOT_NAME.match(path.name)
    )
    removed = []
    for old in snapshots[:-keep]:
        shutil.rmtree(old)
        removed.append(old.name)
        logger.info("Removed old snapshot %s", old)
    return removed


def backup(settings: Settings, log_dir: Path, *, dest: Path | None = None,
           keep: int | None = None, include_db: bool = True,
           dry_run: bool = False, now: datetime | None = None) -> dict:
    """执行一次备份；返回摘要（dry-run 时只返回计划）。"""
    destination = Path(dest) if dest is not None else settings.backup_dir
    keep = settings.backup_keep if keep is None else keep
    snapshot = destination / _snapshot_name(now)
    while snapshot.exists():          # 同一秒内重复执行时避免覆盖
        snapshot = snapshot.with_name(snapshot.name + "-2")

    database = settings.db_path
    plan = {
        "snapshot": str(snapshot),
        "database": include_db and database.exists(),
        "log_dir": str(log_dir),
        "keep": keep,
        "dry_run": dry_run,
    }
    if dry_run:
        return plan

    snapshot.mkdir(parents=True, exist_ok=True)
    result: dict = {"snapshot": str(snapshot), "database": None,
                    "database_bytes": 0, "files": 0, "bytes": 0,
                    "services": {}, "pruned": []}
    if include_db and database.exists():
        target = snapshot / "database" / database.name
        result["database_bytes"] = _copy_database(database, target)
        result["database"] = str(target.relative_to(snapshot))

    files = _copy_logs(log_dir, snapshot / "logs")
    result["files"] = len(files)
    result["bytes"] = sum(item["bytes"] for item in files)
    result["services"] = _group_by_service(files)

    result["pruned"] = _prune_snapshots(destination, keep)
    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot": snapshot.name,
        "source": {"database": str(database), "logs": str(log_dir)},
        **{key: result[key] for key in
           ("database", "database_bytes", "files", "bytes", "services", "pruned")},
        "log_files": files,
    }
    (snapshot / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # 备份动作本身也留痕（写的是源库，不影响快照内容）。
    try:
        if database.exists():
            AuditTrail(Store(database)).append(
                action="backup.create", actor_type="system", actor_name="backup",
                details={"snapshot": snapshot.name, "files": result["files"],
                         "bytes": result["bytes"], "dest": str(destination)})
    except Exception:
        logger.exception("Backup audit failed")
    return result


def _backup_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="备份 Gateway 日志与数据库")
    parser.add_argument("--dest", help="备份根目录（默认 GATEWAY_BACKUP_DIR）")
    parser.add_argument("--keep", type=int, help="保留最近多少个快照（0 表示不清理）")
    parser.add_argument("--no-db", action="store_true", help="只备份日志")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    log_settings = LogSettings.from_env()
    runtime = setup_logging(log_settings, logger_name=__name__)
    try:
        result = backup(settings, log_settings.directory,
                        dest=Path(args.dest) if args.dest else None,
                        keep=args.keep, include_db=not args.no_db,
                        dry_run=args.dry_run)
    finally:
        runtime.stop()

    if args.dry_run:
        print(f"[dry-run] 将备份到 {result['snapshot']}")
        print(f"[dry-run] 数据库: {'有' if result['database'] else '无'}；"
              f"日志目录: {result['log_dir']}")
        return 0
    print(f"备份完成: {result['snapshot']}")
    if result.get("database"):
        print(f"数据库: {result['database_bytes']} 字节")
    else:
        print("数据库: 不存在，已跳过")
    print(f"日志: {result['files']} 个文件，{result['bytes']} 字节")
    for name, group in sorted(result["services"].items()):
        print(f"  - {name}: {group['files']} 个文件，{group['bytes']} 字节")
    if result["pruned"]:
        print(f"已清理旧快照: {', '.join(result['pruned'])}")
    return 0


def _prune_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="清理超过保留期的审计表记录")
    parser.add_argument("--days", type=int, default=None,
                        help="保留天数（默认 GATEWAY_AUDIT_RETENTION_DAYS）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    days = args.days if args.days is not None else settings.audit_retention_days
    store = Store(settings.db_path)
    store.initialize(settings.model)
    removed = prune_audit(store, days, dry_run=args.dry_run)
    if args.dry_run:
        print(f"[dry-run] 将删除 {removed} 条早于 {days} 天的审计记录")
        return 0
    print(f"已删除 {removed} 条早于 {days} 天的审计记录")
    if removed:
        try:
            AuditTrail(store).append(action="audit.prune", actor_type="system",
                                     actor_name="cli",
                                     details={"deleted": removed, "retention_days": days})
        except Exception:
            logger.exception("Prune audit failed")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "prune-audit":
        return _prune_cli(argv[1:])
    if argv and argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    return _backup_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
