from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from . import database
from .models import CollectRequest, CollectResponse, EntryOut, ProjectOut, RunOut
from .service import ServiceError, query_projects, run_collection


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="市政工程投标截止时间采集 API",
    description=(
        "提交公告列表页地址（列表入口），服务抓取全部分页与详情页，将更正公告按"
        "引用的原公告编号归并，并按发布时间确定生效的投标截止时间。"
        "每个入口保存独立快照：重复采集同一入口时，只有分页与详情全部成功、"
        "归并校验通过后，才在同一事务内用本轮公告替换该入口上一版；"
        "撤下的更正在新版中自然消失，不再参与截止时间计算，其他入口不受影响。"
        "任一页面读取或归并失败时保留该入口上一版完整结果。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": "tender-collector", "docs": "/docs"}


@app.post("/api/collect", response_model=CollectResponse, status_code=200)
def collect_endpoint(req: CollectRequest) -> CollectResponse:
    try:
        result = run_collection(req.list_url)
    except ServiceError as exc:
        # 422 语义：采集到的源站内容/可用性有问题，本次未入库
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CollectResponse(status="success", **result)


@app.get("/api/projects", response_model=list[ProjectOut])
def list_projects(
    entry_url: str | None = Query(
        None, description="只查询该列表入口的当前快照；省略则合并全部入口"
    ),
) -> list[ProjectOut]:
    return [ProjectOut(**p) for p in query_projects(entry_url=entry_url)]


@app.get("/api/entries", response_model=list[EntryOut])
def list_entries() -> list[EntryOut]:
    """已保存的列表入口及其最近一次成功采集的快照摘要。"""
    with database.get_conn() as conn:
        return [EntryOut(**dict(r)) for r in database.fetch_entries(conn)]


@app.get("/api/collections", response_model=list[RunOut])
def list_collections(limit: int = Query(20, ge=1, le=200)) -> list[RunOut]:
    with database.get_conn() as conn:
        return [RunOut(**dict(r)) for r in database.fetch_runs(conn, limit)]
