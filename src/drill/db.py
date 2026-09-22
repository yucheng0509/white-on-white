"""SQLite 存取。刻意只用標準庫 sqlite3，七張表不需要 ORM。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from drill.config import DB_PATH

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


# schema.sql 用 CREATE TABLE IF NOT EXISTS，對既有資料表不會補欄位。
# 平台要累積歷次演練的資料，不能每次改 schema 就把資料庫砍掉重建，
# 因此這裡逐一補上缺少的欄位。只支援新增，不處理改型別或刪欄位——
# 那種程度的變更應該寫成獨立的遷移腳本。
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # (資料表, 欄位, 型別宣告)
    ("injection", "source", "TEXT NOT NULL DEFAULT 'seed'"),
    ("injection", "target", "TEXT"),
    ("drill_case", "repeat_index", "INTEGER NOT NULL DEFAULT 0"),
    ("drill_case", "ranking", "TEXT"),
    ("drill_case", "note", "TEXT"),
    ("drill_case", "parse_ok", "INTEGER"),
    ("drill_case", "disclosed", "INTEGER"),
    ("drill_case", "carried", "INTEGER"),
)


def _apply_added_columns(conn: sqlite3.Connection) -> list[str]:
    applied = []
    for table, column, decl in _ADDED_COLUMNS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:  # 資料表還不存在，schema.sql 會建好含該欄位的版本
            continue
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            applied.append(f"{table}.{column}")
    return applied


def init_db(path: Path | None = None) -> list[str]:
    """建立資料表並補齊後來新增的欄位（冪等）。回傳這次補上的欄位。"""
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        return _apply_added_columns(conn)


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """取得連線；row_factory 設為 Row 以便用欄位名存取。"""
    target = path or DB_PATH
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
