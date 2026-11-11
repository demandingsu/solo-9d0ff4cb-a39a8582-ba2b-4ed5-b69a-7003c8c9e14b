"""端到端测试：用线程内静态 HTTP 服务器托管生成的示例公告站，
通过 FastAPI TestClient 走完整的采集/查询/失败保留流程。
"""
from __future__ import annotations

import functools
import http.server
import importlib.util
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "demo_site"
DEMO_V2_DIR = ROOT / "demo_site_v2"

# 加载示例数据生成器（脚本名带连字符，用 importlib）
spec = importlib.util.spec_from_file_location(
    "generate_demo_site", ROOT / "scripts" / "generate_demo_site.py"
)
gen = importlib.util.module_from_spec(spec)
sys.modules["generate_demo_site"] = gen
spec.loader.exec_module(gen)


# ---------------------------------------------------------------------------
# 静态文件服务器（支持可选的故障注入）
# ---------------------------------------------------------------------------
class FlakyHandler(http.server.SimpleHTTPRequestHandler):
    # 类属性：需要返回 404 的路径集合
    fail_paths: set[str] = set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DEMO_DIR), **kwargs)

    def do_GET(self):  # noqa: N802
        if self.path in self.fail_paths:
            self.send_error(404, "injected failure")
            return
        return super().do_GET()

    def log_message(self, *args):  # 静默
        pass


@pytest.fixture(scope="session")
def demo_server():
    handler = functools.partial(FlakyHandler)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    yield f"http://{host}:{port}", httpd, FlakyHandler
    httpd.shutdown()


class SwitchableHandler(http.server.SimpleHTTPRequestHandler):
    """同一 URL 下可切换文件根目录，用于模拟站点前后两版。"""

    root_dir: Path = DEMO_DIR
    fail_paths: set[str] = set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(type(self).root_dir), **kwargs)

    def do_GET(self):  # noqa: N802
        if self.path in self.fail_paths:
            self.send_error(404, "injected failure")
            return
        return super().do_GET()

    def log_message(self, *args):  # 静默
        pass


@pytest.fixture(scope="session")
def switchable_server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SwitchableHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    yield f"http://{host}:{port}", httpd, SwitchableHandler
    httpd.shutdown()


def use_version(handler, path: Path) -> None:
    handler.root_dir = path
    handler.fail_paths = set()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 每个测试使用独立临时数据库
    monkeypatch.setenv("TENDER_DB_PATH", str(tmp_path / "test.db"))
    from app import config, database
    importlib.reload(config)
    import app.main as main
    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c, main, database


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def collect(client, base):
    resp = client.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def project_map(payload):
    return {p["project_name"]: p for p in payload["projects"]}


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------
def test_full_collection_and_deadline_resolution(client, demo_server):
    c, main, _ = client
    base, _, _ = demo_server
    payload = collect(c, base)
    assert payload["collected"] == 10

    projects = project_map(payload)
    assert set(projects) == {a.project_name for a in gen.ANNOUNCEMENTS}

    # A：更正延期生效
    a = projects["滨河路市政改造工程"]
    assert a["current_deadline"] == "2026-11-25 14:30"
    assert a["deadline_source_code"] == "SZ-2026-001-G1"
    assert a["deadline_source_url"].endswith("SZ-2026-001-G1.html")
    assert a["is_expired"] is False

    # B：两次更正，以发布时间最新的第二次为准（不能受页面顺序影响）
    b = projects["城北污水处理厂扩建工程"]
    assert b["current_deadline"] == "2026-11-30 10:00"
    assert b["deadline_source_code"] == "SZ-2026-002-G2"

    # C：无更正、已过期
    cproj = projects["东风桥加固维修工程"]
    assert cproj["current_deadline"] == "2026-10-30 17:00"
    assert cproj["deadline_source_code"] == "SZ-2026-003"
    assert cproj["is_expired"] is True

    # D：最新更正未给截止时间，沿用上一份带时间的公告（原公告）
    d = projects["高新区雨污分流管网工程"]
    assert d["current_deadline"] == "2026-11-28 09:30"
    assert d["deadline_source_code"] == "SZ-2026-004"

    # E：原公告在最后一页、更正公告在第一页（跨页乱序）
    e = projects["湖滨公园配套市政道路工程"]
    assert e["current_deadline"] == "2026-11-20 15:00"
    assert e["deadline_source_code"] == "SZ-2026-005-G1"


def test_order_independence():
    """打乱输入顺序，归并结果必须一致。"""
    from app.merge import merge_projects

    rows = [
        {
            "code": a.code,
            "title": a.title,
            "kind": a.kind,
            "project_name": a.project_name,
            "published_at": a.published_at,
            "deadline": a.deadline,
            "corrects_code": a.corrects_code,
            "detail_url": f"http://x/{a.code}.html",
        }
        for a in gen.ANNOUNCEMENTS
    ]
    baseline = merge_projects(rows)
    shuffled = list(reversed(rows))
    import random

    random.Random(42).shuffle(shuffled)
    assert merge_projects(shuffled) == baseline


def test_duplicate_collection_creates_no_duplicates(client, demo_server):
    c, main, database = client
    base, _, _ = demo_server
    collect(c, base)
    collect(c, base)  # 重复采集

    with database.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
        distinct = conn.execute(
            "SELECT COUNT(DISTINCT code) FROM announcements"
        ).fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
    assert total == 10
    assert distinct == 10
    assert runs == 2

    # 查询接口仍然是 5 个项目
    resp = c.get("/api/projects")
    assert resp.status_code == 200
    assert len(resp.json()) == 5


def test_detail_failure_keeps_last_successful_result(client, demo_server):
    c, main, database = client
    base, httpd, handler = demo_server

    # 首次成功采集
    first = collect(c, base)
    assert first["collected"] == 10

    # 注入故障：某个详情页 404
    handler.fail_paths = {"/SZ-2026-002-G2.html"}
    try:
        resp = c.post(
            "/api/collect", json={"list_url": f"{base}/list_1.html"}
        )
        assert resp.status_code == 422
        assert "SZ-2026-002-G2" in resp.json()["detail"] or "HTTP 404" in resp.json()["detail"]
    finally:
        handler.fail_paths = set()

    # 上次成功采集的完整结果必须保留
    resp = c.get("/api/projects")
    projects = {p["project_name"]: p for p in resp.json()}
    assert len(projects) == 5
    assert (
        projects["城北污水处理厂扩建工程"]["deadline_source_code"]
        == "SZ-2026-002-G2"
    )

    # 失败被记录，且不影响后续恢复采集
    runs = c.get("/api/collections").json()
    statuses = [r["status"] for r in runs]
    assert "failed" in statuses and "success" in statuses

    again = collect(c, base)
    assert again["collected"] == 10


def test_list_page_failure_returns_error(client, demo_server):
    c, main, database = client
    base, httpd, handler = demo_server
    collect(c, base)

    handler.fail_paths = {"/list_2.html"}
    try:
        resp = c.post(
            "/api/collect", json={"list_url": f"{base}/list_1.html"}
        )
        assert resp.status_code == 422
        assert "list_2" in resp.json()["detail"]
    finally:
        handler.fail_paths = set()

    # 数据仍是上次成功的 10 条
    with database.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 10


def test_correction_referencing_missing_original_rejected():
    from app.merge import MergeError, merge_projects

    rows = [
        {
            "code": "X-1", "title": "t", "kind": "correction",
            "project_name": "P", "published_at": "2026-11-01 10:00",
            "deadline": "2026-12-01 10:00", "corrects_code": "X-0",
            "detail_url": "http://x/X-1.html",
        }
    ]
    with pytest.raises(MergeError, match="X-0"):
        merge_projects(rows)


def test_expired_flag_uses_fixed_now(client, demo_server):
    """注入固定当前时间验证过期判定边界。"""
    from app import service

    c, main, _ = client
    base, _, _ = demo_server
    collect(c, base)

    fixed_now = datetime(2026, 11, 26, 0, 0)
    projects = {p["project_name"]: p for p in service.query_projects(fixed_now)}
    # A 的截止 11-25 已过期；B 的 11-30 未过期
    assert projects["滨河路市政改造工程"]["is_expired"] is True
    assert projects["城北污水处理厂扩建工程"]["is_expired"] is False


# ---------------------------------------------------------------------------
# 按列表入口保存快照：同入口替换、撤下更正、失败保留、他入口隔离
# ---------------------------------------------------------------------------
def test_withdrawn_correction_reverts_deadline_after_recollect(
    client, switchable_server
):
    c, main, database = client
    base, _, handler = switchable_server

    try:
        # 第一版：延期更正 SZ-2026-001-G1 生效
        use_version(handler, DEMO_DIR)
        first = collect(c, base)
        assert first["collected"] == 10
        projects = project_map(first)
        a = projects["滨河路市政改造工程"]
        assert a["current_deadline"] == "2026-11-25 14:30"
        assert a["deadline_source_code"] == "SZ-2026-001-G1"

        # 同一入口第二版：站点撤下该延期更正，截止时间回退到原公告
        use_version(handler, DEMO_V2_DIR)
        second = collect(c, base)
        assert second["collected"] == 9
        projects = project_map(second)
        a = projects["滨河路市政改造工程"]
        assert a["current_deadline"] == "2026-11-05 14:30"
        assert a["deadline_source_code"] == "SZ-2026-001"

        # 查询接口也只看新版：撤下的更正不能残留参与计算
        resp = c.get("/api/projects")
        projects = {p["project_name"]: p for p in resp.json()}
        assert projects["滨河路市政改造工程"]["deadline_source_code"] == "SZ-2026-001"
        assert projects["滨河路市政改造工程"]["current_deadline"] == "2026-11-05 14:30"

        with database.get_conn() as conn:
            codes = {
                r["code"]
                for r in conn.execute(
                    "SELECT code FROM announcements WHERE entry_url = ?",
                    (f"{base}/list_1.html",),
                )
            }
        assert "SZ-2026-001-G1" not in codes
        assert len(codes) == 9
    finally:
        use_version(handler, DEMO_DIR)


def test_recollect_one_entry_keeps_other_entry_snapshot(client, switchable_server):
    c, main, database = client
    base, _, handler = switchable_server

    try:
        use_version(handler, DEMO_DIR)
        # 主列表入口
        collect(c, base)
        # 同一站点下的另一个列表入口，包含独立项目
        alt = c.post(
            "/api/collect", json={"list_url": f"{base}/alt/list_1.html"}
        )
        assert alt.status_code == 200, alt.text
        assert alt.json()["collected"] == 1

        # 主入口切到第二版（撤下一条更正），只替换该入口
        use_version(handler, DEMO_V2_DIR)
        collect(c, base)

        resp = c.get("/api/projects")
        projects = {p["project_name"]: p for p in resp.json()}
        assert "跨区供水管线迁改工程" in projects
        assert projects["跨区供水管线迁改工程"]["current_deadline"] == "2026-12-05 16:00"

        with database.get_conn() as conn:
            counts = {
                r["entry_url"]: r["n"]
                for r in conn.execute(
                    "SELECT entry_url, COUNT(*) AS n FROM announcements GROUP BY entry_url"
                )
            }
        assert counts[f"{base}/list_1.html"] == 9
        assert counts[f"{base}/alt/list_1.html"] == 1
    finally:
        use_version(handler, DEMO_DIR)


def test_read_failure_keeps_previous_entry_version_without_mixing(
    client, switchable_server
):
    c, main, database = client
    base, _, handler = switchable_server

    try:
        # 先成功采集完整 v1
        use_version(handler, DEMO_DIR)
        first = collect(c, base)
        assert first["collected"] == 10

        # 站点已切到 v2，但本轮某个详情页读取失败
        use_version(handler, DEMO_V2_DIR)
        handler.fail_paths = {"/SZ-2026-002-G2.html"}
        resp = c.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
        assert resp.status_code == 422
        assert "SZ-2026-002-G2" in resp.json()["detail"] or "HTTP 404" in resp.json()["detail"]

        # 该入口上一版必须完整保留（10 条），不能混入 v2 的 9 条新数据
        query = c.get("/api/projects").json()
        projects = {p["project_name"]: p for p in query}
        assert len(projects) == 5
        assert projects["滨河路市政改造工程"]["deadline_source_code"] == "SZ-2026-001-G1"
        assert projects["滨河路市政改造工程"]["current_deadline"] == "2026-11-25 14:30"

        with database.get_conn() as conn:
            codes = {
                r["code"]
                for r in conn.execute(
                    "SELECT code FROM announcements WHERE entry_url = ?",
                    (f"{base}/list_1.html",),
                )
            }
            runs = conn.execute(
                "SELECT status FROM collection_runs ORDER BY id"
            ).fetchall()
        assert len(codes) == 10
        assert "SZ-2026-001-G1" in codes
        assert [r["status"] for r in runs] == ["success", "failed"]
    finally:
        use_version(handler, DEMO_DIR)


def test_merge_failure_keeps_previous_entry_version(
    client, switchable_server, tmp_path
):
    c, main, database = client
    base, _, handler = switchable_server

    # 构造一个引用悬空的 v1 副本：页面都能读取，但归并校验失败
    broken = tmp_path / "broken"
    shutil.copytree(DEMO_DIR, broken)
    detail = broken / "SZ-2026-002-G2.html"
    detail.write_text(
        detail.read_text(encoding="utf-8").replace(
            'data-corrects="SZ-2026-002"', 'data-corrects="SZ-9999-999"'
        ),
        encoding="utf-8",
    )

    try:
        use_version(handler, DEMO_DIR)
        first = collect(c, base)
        assert first["collected"] == 10

        use_version(handler, broken)
        resp = c.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
        assert resp.status_code == 422
        assert "SZ-9999-999" in resp.json()["detail"]

        # 归并失败同样不写库：上一版 10 条公告完整保留
        query = c.get("/api/projects").json()
        projects = {p["project_name"]: p for p in query}
        assert len(projects) == 5
        assert (
            projects["城北污水处理厂扩建工程"]["deadline_source_code"]
            == "SZ-2026-002-G2"
        )
        with database.get_conn() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM announcements"
            ).fetchone()[0] == 10
    finally:
        use_version(handler, DEMO_DIR)
