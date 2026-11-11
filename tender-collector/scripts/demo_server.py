#!/usr/bin/env python3
"""可切换版本 / 可注入读取故障的本地示例公告站服务器。

用途：在**同一个列表 URL** 下先后提供 v1、v2 两个版本，演示重复采集同一
入口时旧快照被完整替换；并可让某个详情页返回 404，演示读取失败时保留该
入口上一版完整结果。

启动（默认服务 demo_site v1，端口 8001）：
    python3 scripts/demo_server.py --port 8001

演示控制接口（不属于被采集的公告站内容）：
    POST   /__admin/version/v1       切回含延期更正的版本
    POST   /__admin/version/v2       切到已撤下延期更正的版本
    POST   /__admin/fail?path=/SZ-2026-002-G2.html   让指定页面返回 404
    DELETE /__admin/fail             清除所有故障注入
    GET    /__admin/state            查看当前版本与故障注入状态
"""
from __future__ import annotations

import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[SimpleHTTPRequestHandler],
        versions: dict[str, Path],
        initial_version: str,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.versions = versions
        self.current_version = initial_version
        self.fail_paths: set[str] = set()

    @property
    def current_root(self) -> str:
        return str(self.versions[self.current_version])


class DemoHandler(SimpleHTTPRequestHandler):
    server: DemoServer

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # args[2] 是 DemoServer；每个请求都按当前版本选择文件根目录。
        super().__init__(*args, directory=args[2].current_root, **kwargs)

    # -- 基础设施 ---------------------------------------------------------
    def end_headers(self) -> None:
        # 切换版本后浏览器/抓取端不应复用旧缓存。
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _state(self) -> dict[str, Any]:
        return {
            "version": self.server.current_version,
            "root": self.server.current_root,
            "fail_paths": sorted(self.server.fail_paths),
        }

    # -- HTTP 方法 --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/__admin/state":
            self._send_json(200, self._state())
            return
        if parsed.path.startswith("/__admin/"):
            self._send_json(404, {"detail": "unknown admin endpoint"})
            return
        if parsed.path in self.server.fail_paths:
            self.send_error(404, "injected failure")
            return
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/__admin/version/v1", "/__admin/version/v2"):
            version = parsed.path.rsplit("/", 1)[-1]
            self.server.current_version = version
            self._send_json(200, self._state())
            return
        if parsed.path == "/__admin/fail":
            values = parse_qs(parsed.query).get("path", [])
            if not values or not values[0].startswith("/"):
                self._send_json(
                    400, {"detail": "usage: POST /__admin/fail?path=/detail.html"}
                )
                return
            self.server.fail_paths.add(values[0])
            self._send_json(200, self._state())
            return
        self._send_json(404, {"detail": "unknown admin endpoint"})

    def do_DELETE(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/__admin/fail":
            self.server.fail_paths.clear()
            self._send_json(200, self._state())
            return
        self._send_json(404, {"detail": "unknown admin endpoint"})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[demo-site] {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--v1", default=str(ROOT / "demo_site"), help="v1 静态目录")
    parser.add_argument("--v2", default=str(ROOT / "demo_site_v2"), help="v2 静态目录")
    parser.add_argument("--initial", default="v1", choices=("v1", "v2"))
    parser.add_argument(
        "--fail-path",
        action="append",
        default=[],
        help="启动即注入 404 的路径，可重复（如 /SZ-2026-002-G2.html）",
    )
    args = parser.parse_args()

    versions = {"v1": Path(args.v1), "v2": Path(args.v2)}
    for name, path in versions.items():
        if not path.is_dir():
            raise SystemExit(
                f"{name} 目录不存在: {path}\n请先运行 python3 scripts/generate_demo_site.py"
            )

    server = DemoServer(
        (args.host, args.port), DemoHandler, versions, args.initial
    )
    server.fail_paths.update(args.fail_path)
    host, port = server.server_address[:2]
    print(
        f"demo site ({args.initial}) at http://{host}:{port}/list_1.html "
        f"(admin: /__admin/state)"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
