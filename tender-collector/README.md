# 市政工程投标截止时间采集服务

面向招投标专员的公告采集 API：提交市政工程公告站的**列表页地址**（下称“列表
入口”），服务自动遍历所有分页与详情页，将**更正公告按其引用的原公告编号归并**
到同一项目，并**按发布时间**（而非页面排列顺序）确定当前生效的投标截止时间，
标记是否已过期。

每个列表入口保存一份**独立、完整的公告快照**：

- 只有本轮分页与详情**全部采集成功、归并校验通过**后，才在同一个数据库事务内
  用本轮公告替换该入口上一版快照；
- 站点撤下的公告（例如已撤下的延期更正）在新版快照中自然消失，不再参与截止
  时间计算；
- 采集其他入口不会删除或改写本入口已保存的公告；
- 任一页面读取失败或归并失败时，本入口上一版完整保留，查询不会出现新旧公告
  混合。

## 核心规则

1. **分页遍历**：从提交的列表页出发，沿 `nav.pagination` 链接抓取全部列表页
   （去重、防环、限量），收集所有详情页链接后逐页抓取。
2. **更正归并**：更正公告通过“原公告编号”归并到对应原公告所在项目；引用悬空
   （原公告不存在/不是原公告）视为数据错误，本次采集整体失败。
3. **生效截止时间**：同一项目内按**发布时间**排序，取发布时间最新且声明了
   截止时间的公告；发布时间相同时按公告编号确定次序，结果与页面顺序完全无关。
   若最新更正公告未给出截止时间（例如只改联系人），沿用上一份带截止时间的公告。
4. **过期判定**：`is_expired = 截止时间 <= 当前时间`（截止时间按服务器本地时区
   的 naive 时间比较，与公告页 `YYYY-MM-DD HH:MM` 格式一致）。
5. **按入口保存**：公告记录以 `(入口地址, 公告编号)` 为唯一键。重复采集同一入口
   会**整体替换**该入口快照而不是简单追加，因此已撤下的公告不会残留；不同入口
   之间互不影响。
6. **原子替换与失败保留**：抓取和归并全部成功后才进入写库事务；事务内先删除
   本入口旧快照、再写入本轮完整快照，提交前查询只能看到旧版，提交后只能看到
   新版。任一列表页/详情页读取失败（网络错误、非 2xx、字段缺失）或归并校验
   失败时，公告表完全不动，本入口上一版完整保留；失败原因记入
   `collection_runs` 表并通过 HTTP 422 返回。

## 目录结构

```
tender-collector/
├── app/                    # FastAPI 应用
│   ├── main.py             # 路由：/api/collect、/api/projects、/api/entries、/api/collections
│   ├── service.py          # 采集编排 + 替换事务 + 过期判定
│   ├── scraper.py          # httpx + BeautifulSoup 抓取/解析
│   ├── merge.py            # 更正归并与生效截止时间判定（纯函数）
│   ├── database.py         # SQLite 连接、按入口快照的建表/替换/查询
│   ├── models.py           # Pydantic 模型
│   └── config.py           # 环境变量配置
├── demo_site/              # 示例站版本 v1（含延期更正），另有 alt/ 第二入口
├── demo_site_v2/           # 示例站版本 v2（同一入口已撤下延期更正）
├── scripts/
│   ├── generate_demo_site.py  # 生成 v1/v2 示例站（数据可复现）
│   └── demo_server.py         # 同一 URL 切换 v1/v2 + 注入读取故障的本地服务器
├── tests/test_collection.py   # 端到端 + 归并单元测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
# 1. 生成示例公告站的 v1/v2 静态文件（已随仓库提供，可重复生成）
python3 scripts/generate_demo_site.py

# 2. 启动：API 在 :8000，可切换版本的示例公告站在 :8001
docker compose up --build -d

# 3. 首次采集 v1（默认版本）
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://demo-site/list_1.html"}'

# 4. 查询项目当前截止时间：滨河路项目为延期后的 2026-11-25 14:30
curl http://localhost:8000/api/projects
```

容器内 API 通过 compose 网络访问示例站（主机名 `demo-site`，端口 80）；从宿主机
浏览示例站则打开 <http://localhost:8001/list_1.html>。SQLite 数据存于命名卷
`tender-data`（容器内 `/data/tenders.db`）。

### 演示：同一入口前后两版（撤下延期更正）

示例站 v2 撤下了项目 A 的延期更正公告 `SZ-2026-001-G1`。切换版本后**仍用同一个
列表 URL** 重新采集：

```bash
# 切换到 v2（同一 URL 内容变化）
curl -X POST http://localhost:8001/__admin/version/v2

# 重新采集同一入口
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://demo-site/list_1.html"}'

# 查询：滨河路项目回退到原公告截止时间 2026-11-05 14:30，
# 来源公告编号变为 SZ-2026-001；撤下的 G1 不再参与计算
curl http://localhost:8000/api/projects
```

切回 v1：

```bash
curl -X POST http://localhost:8001/__admin/version/v1
```

### 演示：读取故障时保留该入口上一版

在 v2 已成功采集后，让某个详情页返回 404，再采集同一入口：

```bash
# 注入故障（页面仍在列表中，但读取返回 404）
curl -X POST 'http://localhost:8001/__admin/fail?path=/SZ-2026-002-G2.html'

# 本次采集返回 422
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://demo-site/list_1.html"}'

# 查询结果仍是上一版完整快照（9 条公告、5 个项目），没有新旧混合
curl http://localhost:8000/api/projects

# 清除故障
curl -X DELETE http://localhost:8001/__admin/fail
```

### 演示：其他入口不受影响

示例站的 `alt/` 是另一个列表入口（独立项目）：

```bash
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://demo-site/alt/list_1.html"}'

# 随后无论怎样重新采集主入口，alt 入口的项目都保留
curl http://localhost:8000/api/entries
curl 'http://localhost:8000/api/projects?entry_url=http://demo-site/alt/list_1.html'
```

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除采集数据
```

## 本地开发运行（不使用 Docker）

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/generate_demo_site.py

# 终端 A：示例公告站（同一 URL 可切 v1/v2、可注入故障）
python3 scripts/demo_server.py --port 8001

# 终端 B：API（数据库默认在 ./data/tenders.db）
uvicorn app.main:app --reload --port 8000

# 终端 C：测试
pytest -q

# 采集（v1 默认）
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://127.0.0.1:8001/list_1.html"}'
```

交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>

## API 说明

### `POST /api/collect` — 提交列表入口，触发采集

请求：

```json
{ "list_url": "http://demo-site/list_1.html" }
```

成功响应（200）：

```json
{
  "status": "success",
  "entry_url": "http://demo-site/list_1.html",
  "source_url": "http://demo-site/list_1.html",
  "collected": 10,
  "projects": [
    {
      "project_name": "城北污水处理厂扩建工程",
      "current_deadline": "2026-11-30 10:00",
      "deadline_source_code": "SZ-2026-002-G2",
      "deadline_source_url": "http://demo-site/SZ-2026-002-G2.html",
      "is_expired": false
    }
  ]
}
```

`projects` 是本轮该入口快照的归并结果；只有本次采集完全成功后，才会替换该入口
上一版。

失败响应（422，任一页面不可读/数据无法归并；该入口旧快照保持不变）：

```json
{ "detail": "读取页面失败 http://demo-site/SZ-2026-002-G2.html: HTTP 404" }
```

### `GET /api/projects` — 查询当前截止时间

默认合并所有列表入口的当前快照。同一公告编号出现在多个入口时，取最近一次成功
采集的副本。可选 `entry_url` 只查询某个入口：

```
GET /api/projects?entry_url=http://demo-site/list_1.html
```

返回字段：项目名称、当前生效截止时间、截止时间来源公告编号、来源链接、是否过期。

### `GET /api/entries` — 已保存的入口快照

返回每个入口最近一次成功采集的公告条数与更新时间。

### `GET /api/collections?limit=20` — 采集运行历史

包含每次采集的成功/失败状态、采集条数与错误信息。

## 示例站数据设计

`demo_site/` 为版本 v1：5 个项目、10 条公告、3 个列表页，列表按发布时间**倒序**
排列（更正公告排在原公告之前），专门用于验证归并不受页面顺序影响。
`demo_site_v2/` 为同一入口撤下 `SZ-2026-001-G1` 后的版本：9 条公告。

| 项目 | 场景 | v1 生效截止时间来源 | v2（撤下 G1 后） |
| --- | --- | --- | --- |
| 滨河路市政改造工程 | 一次延期更正；v2 撤下该更正 | 更正公告 SZ-2026-001-G1（11-25） | 原公告 SZ-2026-001（11-05） |
| 城北污水处理厂扩建工程 | 两次更正，取最新一次 | 第二次更正 SZ-2026-002-G2 | 同左 |
| 东风桥加固维修工程 | 无更正，已过期 | 原公告 SZ-2026-003 | 同左 |
| 高新区雨污分流管网工程 | 更正不改截止时间 | 原公告 SZ-2026-004 | 同左 |
| 湖滨公园配套市政道路工程 | 原公告在末页、更正在首页（跨页乱序） | 更正公告 SZ-2026-005-G1 | 同左 |

`demo_site/alt/`（以及 v2 中的副本）是独立列表入口，仅含项目“跨区供水管线迁改
工程”，用于演示入口之间互不影响。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TENDER_DB_PATH` | 本地 `./data/tenders.db`；容器内 `/data/tenders.db` | SQLite 文件路径 |
| `TENDER_HTTP_TIMEOUT` | `15` | 单个页面请求超时（秒） |
| `TENDER_MAX_LIST_PAGES` | `200` | 单次采集最多遍历的列表页数（防环） |
| `TENDER_USER_AGENT` | `TenderCollector/1.0 ...` | 请求 UA |

> 从旧版（全局按公告编号保存）升级时，旧 `announcements` 表会被重命名为
> `announcements_legacy*` 备份且不再参与查询（旧表没有入口信息，无法安全归属）；
> 重新采集各入口后会重新生成按入口保存的快照。

## 解析约定（接入真实站点时）

解析器依赖以下稳定选择器，适配其他站点时调整 `app/scraper.py` 即可：

- 列表页：`ul.announcement-list a.detail-link` 为详情链接，
  `nav.pagination a` 为分页链接；
- 详情页：`article.announcement-detail` 容器，支持
  `data-code` / `data-kind` / `data-published-at` / `data-corrects` 属性，
  以及 `table.detail-table` 内 `th[data-field="字段名"] + td` 表格字段
  （公告编号、公告类型、项目名称、发布时间、投标截止时间、原公告编号）。
