"""SQLite 持久化层（按“列表入口”保存公告快照）。

数据模型
--------
* ``announcements``：一条记录 = 某个列表入口 (entry_url) 当前版本中的一条公告。
  主键为 (entry_url, code)，因此不同入口可以各自保存同一个公告编号，
  某个入口撤下公告时不会影响其他入口。
* ``collection_entries``：记录每个入口最近一次成功发布的快照摘要。
* ``collection_runs``：每次采集的成败历史（成功/失败都会记录）。

一次成功采集在**同一个事务**内先删除该入口旧快照、再写入本轮完整快照，
提交后查询才能看到新版本；失败时事务不写公告表，旧快照原样保留。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    entry_url       TEXT NOT NULL,
    code            TEXT NOT NULL,
    title           TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('original', 'correction')),
    project_name    TEXT NOT NULL,
    published_at    TEXT NOT NULL,
    deadline        TEXT,
    corrects_code   TEXT,
    detail_url      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    PRIMARY KEY (entry_url, code)
);
CREATE INDEX IF NOT EXISTS idx_announcements_code
    ON announcements(code);
CREATE INDEX IF NOT EXISTS idx_announcements_project
    ON announcements(project_name);

CREATE TABLE IF NOT EXISTS collection_entries (
    entry_url       TEXT PRIMARY KEY,
    items_collected INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    items_collected INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path or settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_legacy_announcements(conn: sqlite3.Connection) -> None:
    """把旧版“全局唯一 code”表移走，避免旧数据继续参与查询。

    旧表没有入口信息，无法判断它属于哪个入口，因此不迁移数据；保留为
    备份表（不删除），新版查询只使用新表。重新采集各入口后会恢复数据。
    """
    if "announcements" not in {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }:
        return
    if "entry_url" in _table_columns(conn, "announcements"):
        return

    backup = "announcements_legacy"
    if backup in {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup = f"announcements_legacy_{stamp}"
    # 索引随旧表保留，先删掉同名索引，SCHEMA 才能在新表上重新创建。
    conn.execute("DROP INDEX IF EXISTS idx_announcements_project")
    conn.execute("DROP INDEX IF EXISTS idx_announcements_corrects")
    conn.execute(f'ALTER TABLE announcements RENAME TO "{backup}"')


def init_db(db_path: str | None = None) -> None:
    with get_conn(db_path) as conn:
        _migrate_legacy_announcements(conn)
        conn.executescript(SCHEMA)
        conn.commit()


def utcnow_iso() -> str:
    # 微秒精度用于跨入口同编号公告的去重排序（新采集者优先）。
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def replace_entry_snapshot(
    conn: sqlite3.Connection, entry_url: str, rows: list[dict[str, Any]]
) -> None:
    """在同一事务内用本轮完整公告替换该入口上一版快照。

    调用方负责提交/回滚：本函数只执行 DELETE + INSERT，任何异常都会让
    调用方回滚，从而保证查询永远看不到新旧混合的数据。
    """
    now = utcnow_iso()
    conn.execute("DELETE FROM announcements WHERE entry_url = ?", (entry_url,))

    # 公告编号在同一入口内唯一；归并层已校验引用完整性。
    unique_rows = {r["code"]: r for r in rows}
    ordered = sorted(
        unique_rows.values(), key=lambda r: (0 if r["kind"] == "original" else 1, r["code"])
    )
    conn.executemany(
        """
        INSERT INTO announcements
            (entry_url, code, title, kind, project_name, published_at, deadline,
             corrects_code, detail_url, last_seen_at)
        VALUES
            (:entry_url, :code, :title, :kind, :project_name, :published_at, :deadline,
             :corrects_code, :detail_url, :last_seen_at)
        """,
        [{**r, "entry_url": entry_url, "last_seen_at": now} for r in ordered],
    )
    conn.execute(
        """
        INSERT INTO collection_entries (entry_url, items_collected, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(entry_url) DO UPDATE SET
            items_collected=excluded.items_collected,
            updated_at=excluded.updated_at
        """,
        (entry_url, len(ordered), now),
    )


def record_run(
    conn: sqlite3.Connection,
    *,
    source_url: str,
    status: str,
    items_collected: int,
    error: str | None,
    started_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO collection_runs
            (source_url, status, items_collected, error, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (source_url, status, items_collected, error, started_at, utcnow_iso()),
    )


def fetch_entry_announcements(
    conn: sqlite3.Connection, entry_url: str
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM announcements
            WHERE entry_url = ?
            ORDER BY project_name, published_at, code
            """,
            (entry_url,),
        )
    )


def fetch_all_announcements(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """读取全部入口的当前快照。

    同一公告编号可能同时出现在多个入口中（入口之间互不影响），查询时按
    ``last_seen_at`` 取最近一次成功采集的副本，避免归并层把同编号当成重复。
    """
    rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT * FROM announcements
            ORDER BY last_seen_at, entry_url
            """
        )
    ]
    latest_by_code: dict[str, dict[str, Any]] = {}
    for row in rows:  # 升序迭代，后面的即更新
        latest_by_code[row["code"]] = row
    return sorted(
        latest_by_code.values(),
        key=lambda r: (r["project_name"], r["published_at"], r["code"]),
    )


def fetch_entries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM collection_entries ORDER BY entry_url"
        )
    )


def fetch_runs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM collection_runs ORDER BY id DESC LIMIT ?", (limit,)
        )
    )
