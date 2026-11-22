"""FlowGate 数据库备份工具。

用法：
    .venv/Scripts/python.exe backup_db.py [备份目录]

使用 SQLite 官方在线备份 API，可在服务运行时安全执行，生成带时间戳的快照。
建议用计划任务每天执行一次。
"""
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "flowgate.db"


def backup(target_dir: Path) -> Path:
    if not DB.exists():
        raise FileNotFoundError(f"数据库不存在: {DB}")
    target_dir.mkdir(parents=True, exist_ok=True)
    name = time.strftime("flowgate-%Y%m%d-%H%M%S.db")
    dest = target_dir / name

    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def prune(target_dir: Path, keep: int = 7) -> int:
    """只保留最近 keep 份备份。"""
    files = sorted(target_dir.glob("flowgate-*.db"), reverse=True)
    removed = 0
    for old in files[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "backups"
    out = backup(target)
    removed = prune(target, 7)
    print(f"备份完成: {out} ({out.stat().st_size / 1048576:.2f} MB)")
    if removed:
        print(f"已清理 {removed} 份旧备份（保留最近 7 份）")
