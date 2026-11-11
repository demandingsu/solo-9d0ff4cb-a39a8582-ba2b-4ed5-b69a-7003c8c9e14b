"""采集编排服务：抓取 -> 归并校验 -> 单事务替换该入口快照。

关键事务边界：
1. 分页与详情全部抓取成功、归并校验通过之前，不写任何公告数据；
2. 成功时在**同一事务**内删除该入口旧快照并写入本轮完整快照，提交后
   查询只会看到该入口的旧版或新版，绝不会出现新旧混合；
3. 任一页面读取失败或归并失败时，公告表不动，该入口上一版完整保留，
   其他入口已保存的公告也不受影响。
"""
from __future__ import annotations

from datetime import datetime

from . import database
from .merge import EffectiveDeadline, MergeError, merge_projects
from .scraper import CollectionError, collect

TIME_FORMAT = "%Y-%m-%d %H:%M"


class ServiceError(Exception):
    """对外统一的采集错误（带可读原因）。"""


def _is_expired(deadline_text: str, now: datetime | None = None) -> bool:
    try:
        deadline = datetime.strptime(deadline_text, TIME_FORMAT)
    except ValueError:
        # 容错：支持带秒或 ISO 形式
        deadline = datetime.fromisoformat(deadline_text)
    return deadline <= (now or datetime.now())


def _to_project(d: EffectiveDeadline, now: datetime | None = None) -> dict:
    return {
        "project_name": d.project_name,
        "current_deadline": d.deadline,
        "deadline_source_code": d.source_code,
        "deadline_source_url": d.source_url,
        "is_expired": _is_expired(d.deadline, now),
    }


def build_projects(rows: list[dict], now: datetime | None = None) -> list[dict]:
    return [_to_project(d, now) for d in merge_projects(rows)]


def run_collection(list_url: str) -> dict:
    started_at = database.utcnow_iso()
    try:
        raw = collect(list_url)
        # 先在内存完成归并校验：数据有问题时不会写库。
        projects = build_projects(raw)
    except (CollectionError, MergeError) as exc:
        # 记录失败采集；announcements 表完全不动，该入口上一版完整保留。
        with database.get_conn() as conn:
            database.record_run(
                conn,
                source_url=list_url,
                status="failed",
                items_collected=0,
                error=str(exc),
                started_at=started_at,
            )
            conn.commit()
        raise ServiceError(str(exc)) from exc

    # 成功：单事务“替换该入口快照 + 运行记录”，保证原子性。
    with database.get_conn() as conn:
        try:
            database.replace_entry_snapshot(conn, list_url, raw)
            database.record_run(
                conn,
                source_url=list_url,
                status="success",
                items_collected=len(raw),
                error=None,
                started_at=started_at,
            )
            conn.commit()
        except Exception as exc:  # pragma: no cover - 兜底回滚
            conn.rollback()
            raise ServiceError(f"写入数据库失败: {exc}") from exc

    return {
        "entry_url": list_url,
        "source_url": list_url,
        "collected": len(raw),
        "projects": projects,
    }


def query_projects(
    now: datetime | None = None, entry_url: str | None = None
) -> list[dict]:
    with database.get_conn() as conn:
        if entry_url is None:
            rows = [dict(r) for r in database.fetch_all_announcements(conn)]
        else:
            rows = [
                dict(r)
                for r in database.fetch_entry_announcements(conn, entry_url)
            ]
    if not rows:
        return []
    return build_projects(rows, now)
