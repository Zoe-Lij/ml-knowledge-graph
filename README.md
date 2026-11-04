# 机器学习领域知识图谱系统

> 课程设计项目 · Neo4j 图数据库 + Python(FastAPI/py2neo) + D3.js 前端 + 自然语言问答
> 版本 1.0.0 ｜ 4 人小组 ｜ 7 天周期 ｜ 最小可用系统（MVP）
>
> 注：课程文档、汇报 PPT、截图等交付材料作为课程作业单独提交，**不随本仓库分发**；
> 本仓库只保留可运行的工程代码与数据集。文中提到的 `docs/xxx.md` 指本地交付材料。

一个面向机器学习课程知识点的领域知识图谱：把「监督学习 → 分类任务 → 逻辑回归 → 准确率」这类关系
存进 Neo4j，用网页力导向图可视化，并支持用**自然语言提问**（例如「哪些算法可以做异常检测？」），
由规则 + 模板把问句翻译成 Cypher 查询图数据库，再用中文回答并高亮命中子图。

![首页](docs/screenshots/04_home.png)

![问答效果](docs/screenshots/04_qa_path.png)

---

## 1. 功能特性

| 模块 | 能力 |
| --- | --- |
| 知识图谱 | 113 个实体、258 条关系，4 类实体（算法 / 模型 / 评价指标 / 机器学习概念）× 5 类关系（属于 / 衍生自 / 基于 / 可对比 / 应用于） |
| 图谱可视化 | D3.js 力导向图：节点拖拽固定、悬浮显示实体信息、点击看详情、滚轮缩放、按类型筛选、低置信边虚线标记 |
| 自然语言问答 | 8 类问句意图（实体详情 / 一跳关系 / 条件筛选 / 双实体对比 / 多跳路径 / 类型列表 / 统计 / 关键词兜底），返回中文解释 + 实际执行的 Cypher |
| 安全防护 | 四层 Cypher 注入防护（问句筛查 / 只读校验 / 标签与关系白名单 / 全参数化），拦截记录可查 |
| 可追溯性 | 每条关系带 `confidence` 与 `evidence`（0.98 课程原文 / 0.92 领域定义 / 0.86 结构补全 / 0.78 推理补全） |
| 演示兜底 | 数据库连不上自动降级本地 `kg.json`（`KG_MODE=auto`），断网也能跑（D3 已本地化） |

---

## 2. 技术栈与版本（重要）

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.9 ~ 3.13 | 开发与实测环境为 3.13.12 |
| **Neo4j** | **4.4.x LTS** | ⚠️ 不要用 5.x，见下方说明 |
| **py2neo** | **2021.2.4** | 课程指定驱动，最后与 4.4 稳定配套的版本 |
| FastAPI | 0.156.4 | 后端 Web 框架 |
| uvicorn | 0.54.0 | ASGI 服务器 |
| python-dotenv | 1.2.4 | 读取 `.env` 配置 |
| D3.js | 7.9.0 | 已本地化到 `web/vendor/`，无需联网 |

> **为什么必须锁 Neo4j 4.4**：Neo4j 5.x 修改了 Bolt 协议握手方式，py2neo 2021.2.4 连接时会报
> `No matching Bolt version` 或 `Cannot decode response`。这不是代码 bug，换回 4.4.x 即正常。
> 详见 `docs/02_neo4j_setup.md` 第 1 节。

---

## 3. 快速开始（已装好 Python 的话，三条命令）

```bash
cd ~/研一/机器学习/课程设计     # 完整路径，不要只敲"课程设计"
./start.sh                      # 首次会自动建 .venv 并装依赖，然后启动服务
open http://127.0.0.1:8000/     # 浏览器打开
```

> 更简单的方式：在 Finder 里打开项目文件夹，**双击 `启动系统.command`** 即可（等效于上面三步）。
> 终端里判断目录是否正确：`ls` 能看到 `start.sh`、`app`、`web` 就对了。

没有 Neo4j 也能跑：默认 `KG_MODE=auto`，连不上数据库时自动用 `data/kg.json` 计算，
页面右上角显示「本地图（Neo4j 未连接）」，所有功能（可视化 + 问答）照常可用。

---

## 4. 完整部署步骤

### 4.1 环境准备

| 项 | 要求 | 检查命令 |
| --- | --- | --- |
| Python | ≥ 3.9 | `python3 -V` |
| pip | 随 Python 自带 | `python3 -m pip -V` |
| Neo4j Desktop | 4.4.x（可选，演示建议装） | 打开 App 能看到 DBMS 列表 |
| 端口 | 8000（后端）、7474/7687（Neo4j）空闲 | `lsof -i:8000` / `lsof -i:7687` |

### 4.2 安装 Python 依赖

**方式 A：一键脚本（推荐）**

```bash
cd /Users/zoe/研一/机器学习/课程设计
./start.sh
```

脚本逻辑：`.venv` 不存在 → 自动 `python3 -m venv .venv` + `pip install -r requirements.txt` → 启动服务。
端口被占用时会给提示，不会直接抛 ERROR。

**方式 B：手动**

```bash
cd /Users/zoe/研一/机器学习/课程设计
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> `requirement.txt` 与 `requirements.txt` 内容一致（前者是课程交付要求的文件名），
> 安装统一用 `requirements.txt`。

### 4.3 创建 Neo4j 数据库（Neo4j Desktop）

> 没有 Neo4j 可跳过本节，系统会以本地模式运行。

1. 打开 **Neo4j Desktop** → 左上 `Projects` → 新建或选中一个项目；
2. 点 **Add** → **Local DBMS**；
3. 填写：
   - **Name**：`mlkg`（随意）
   - **Version**：**4.4.x**（关键，不要选 5.x）
   - **Password**：`你的Neo4j密码`（与 `.env` 默认值一致，省去改配置）
4. 点 **Create**，等待初始化完成 → 点 **Start**，状态变为 **Running**；
5. 点 **Open** 打开浏览器界面 `http://localhost:7474`，用 `neo4j / 你的Neo4j密码` 登录；
6. 在 Browser 里执行 `RETURN 1;` 确认可用。

**端口约定**

| 用途 | 地址 | 说明 |
| --- | --- | --- |
| 浏览器界面 | `http://localhost:7474` | 人工看图谱、敲 Cypher |
| 程序连接（Bolt） | `bolt://localhost:7687` | Python 必须用这个 |

### 4.4 配置连接信息

```bash
cp .env.example .env
```

`.env` 关键项：

```ini
NEO4J_URI=bolt://localhost:7687   # 注意 bolt:// 前缀，不是 http://
NEO4J_USER=neo4j
NEO4J_PASSWORD=你的Neo4j密码           # 与创建 DBMS 时填的一致
KG_MODE=auto                      # auto / neo4j / local
API_HOST=127.0.0.1
API_PORT=8000
```

| KG_MODE | 行为 | 适用场景 |
| --- | --- | --- |
| `auto`（默认） | 优先查 Neo4j，连不上自动降级本地图，响应里带 `degraded=true` | 演示（最稳） |
| `neo4j` | 只查数据库，连不上直接 503 | 验收「确实在查图库」 |
| `local` | 只查 `data/kg.json` | 没装数据库时讲解系统逻辑 |

### 4.5 导入数据

**方式 A：py2neo 脚本（推荐）**

```bash
# 先预览（不连数据库，检查 113 实体 / 258 关系是否正确）
.venv/bin/python scripts/import_neo4j.py --dry-run

# 正式导入（--reset 会先清空同名图数据）
.venv/bin/python scripts/import_neo4j.py --reset

# 校验连接与数据量
.venv/bin/python scripts/check_connection.py
```

期望输出：节点 **113**、关系 **258**，类型分布 38/34/23/18。

**方式 B：纯 Cypher（LOAD CSV，在 Neo4j Browser 里执行）**

```bash
# 1) 把 CSV 拷进数据库的 import 目录
#    Desktop: 每个 DBMS 右侧 ... → Open folder → Import
cp data/entities.csv data/relations.csv "<NEO4J_IMPORT_DIR>/"
```

然后按顺序在 Browser 里执行：

| 顺序 | 文件 | 作用 |
| --- | --- | --- |
| 1 | `cypher/00_reset.cypher` | 清空数据库 |
| 2 | `cypher/01_schema.cypher` | 建唯一约束与索引 |
| 3 | `cypher/02_import_loadcsv.cypher` | 批量导入节点与关系（含置信度） |
| 4 | `cypher/03_verify.cypher` | 9 项校验 + 置信度分布 |

> 导入关系用 `relations.csv`（含 `confidence` / `evidence` / `symmetric`），不是三列的
> `entity_relation.csv`。后者是阶段1 的交付主表，严格保持 `head,relation,tail` 三列。

### 4.6 启动后端

```bash
cd /Users/zoe/研一/机器学习/课程设计
./start.sh
# 或： .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

成功标志：

```
[2/2] 启动服务：http://127.0.0.1:8000/  （Ctrl+C 停止）
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

> 服务跑在前台，**不要关闭终端窗口**；关闭即停止服务。

### 4.7 打开前端页面

浏览器访问 **<http://127.0.0.1:8000/>**（前端已由 FastAPI 托管，无需另起服务）。

页面操作：

- 顶部徽标：`Neo4j 已连接`（绿）/ `本地图（Neo4j 未连接）`（黄）；
- 图谱区：拖拽节点、滚轮缩放、悬浮看信息、点击看详情、左上图例可隐藏类型；
- 问答区：输入问题或点示例，答案在下方显示，命中节点会在图上高亮定位；
- 展开「查询证据与执行细节」可看到实际执行的 Cypher 与参数（答辩重点）。

其他入口：接口文档 <http://127.0.0.1:8000/docs>、健康检查 `/api/health`。

### 4.8 验收自测（4 条命令）

```bash
.venv/bin/python tools/validate_dataset.py                       # 数据体检（规模/分布/连通性）
.venv/bin/python scripts/import_neo4j.py --dry-run               # 导入预览
.venv/bin/python scripts/smoke_test_api.py                       # 接口自测（23 项）
.venv/bin/python scripts/security_selftest.py --base-url http://127.0.0.1:8000  # 注入防护（44 项）
```

期望：全部 PASS。数据体检会打印实体/关系分布、孤立节点、弱连通分量数。

### 4.9 线上部署（Vercel，前后端一体上线 · 零配置）

线上不是"只传前端"：前端页面由 FastAPI 的 `StaticFiles` 托管，后端跑在 Vercel Serverless
（入口 `index.py`，业务代码与本地完全一致），数据是随包分发的 `data/kg.json`（113 实体 / 258 关系），
由**服务端**加载——刷新、换电脑、清缓存都不会丢，也不依赖任何浏览器本地存储。

```bash
# 1) 推代码（GitHub Desktop：Add Local Repository → Publish，建议 Private）
# 2) Vercel → Add New Project → 选该仓库 → Framework 选 Other → 不填任何环境变量 → Deploy
```

部署后自检（把域名换成自己的）：

```bash
curl -s $BASE/api/health | python3 -m json.tool      # 113 / 258，store=kg.json
curl -s $BASE/api/graph | head -c 200                # 113 nodes / 258 links
```

> 可选升级：拿到公网 Neo4j（Aura / 云服务器）后，在 Vercel 加 5 个环境变量
> （`NEO4J_URI/USER/PASSWORD/DATABASE`、`KG_MODE=auto`）并 Redeploy，即可切到真实图库，
> `/api/health` 的 `store` 会变成 `neo4j`，**代码无需改动**。

详见 `docs/06_vercel_deploy.md`（含 8 项验收清单、常见报错对照、AI 用量声明）。

> 两个驱动的分工：本机 Neo4j 4.4 用 `py2neo`（`bolt://`），云端 5.x 用官方驱动（`neo4j+s://`），
> `store.py` 按 URI 自动切换——因为 py2neo 2021.2.4 在 5.x 上握手就失败。

---

## 5. 项目目录结构

```
课程设计/
├── app/                        后端服务（FastAPI，约 1900 行）
│   ├── __init__.py
│   ├── config.py               配置中心：读 .env，KG_MODE / 端口 / 查询上限
│   ├── kgdata.py               加载 data/kg.json：实体词典、邻接表、度数、边置信度索引
│   ├── nl2cypher.py            ★核心：实体识别 + 意图分类 + 8 个 Cypher 模板
│   ├── store.py                双执行后端：Neo4jStore(py2neo) / LocalStore(内存图)
│   ├── explain.py              查询结果 → 中文解释 + 命中子图（前端高亮用）
│   ├── security.py             ★Cypher 安全护栏：输入筛查 / 只读校验 / 白名单 / 审计
│   └── main.py                 路由（/api/graph、/api/query 等）、参数校验、静态托管
│
├── web/                        前端（单页，无构建）
│   ├── index.html              D3 力导向图 + 问答界面（1338 行，含注释）
│   └── vendor/d3.v7.min.js     D3 v7.9.0 本地副本（断网可用）
│
├── data/                       数据集
│   ├── entity_relation.csv     ★阶段1 主交付：258 条三元组，字段严格三列 head,relation,tail
│   ├── entities.csv            113 个实体 + 类型/别名/描述/置信度（辅助表，建库与实体识别用）
│   ├── relations.csv           258 条关系 + 头尾类型/symmetric/置信度/依据（辅助表）
│   ├── low_confidence_relations.csv  77 条待人工复核的关系清单（置信度 < 0.80）
│   └── kg.json                 聚合数据，后端与前端兜底直接读取
│
├── cypher/                     Cypher 脚本（Neo4j Browser 可直接执行）
│   ├── 00_reset.cypher         清空数据库（可重复执行）
│   ├── 01_schema.cypher        唯一约束 + 索引
│   ├── 02_import_loadcsv.cypher  LOAD CSV 批量导入（4 段节点 + 5 段关系）
│   ├── 03_verify.cypher        9 项数据校验 + 置信度分布
│   └── 04_demo_queries.cypher  10 条演示查询（5 种关系 / 最短路径 / 统计）
│
├── scripts/                    运维与自测脚本
│   ├── import_neo4j.py         ★py2neo 一键入库（--dry-run / --reset / --batch）
│   ├── check_connection.py     连接自检：连通性 + 数量 + 分布 + 抽样路径
│   ├── smoke_test_api.py       接口冒烟测试（标准库 urllib，无额外依赖）
│   └── security_selftest.py    注入防护自测（12 恶意问句 + 19 语句 + 13 HTTP）
│
├── tools/                      数据构建与体检
│   ├── build_dataset.py        ★数据单一数据源：实体/关系常量 + 一键导出 4 个文件
│   └── validate_dataset.py     数据体检报告（规模/分布/连通性/别名/置信度）
│
├── docs/                       课程文档
│   ├── 01_schema_design.md     Schema 设计说明书（实体判定规则、关系语义、NL2Cypher 设计）
│   ├── 02_neo4j_setup.md       Neo4j 环境配置、两条导入路径、9 条错误对照表
│   ├── 03_api_reference.md     接口文档（字段表、意图表、错误码、安全四层）
│   ├── 04_optimization_design.md  优化设计（安全护栏 + 置信度标注，= 设计报告第 6/7 章原稿）
│   ├── 05_frontend_design.md   前端设计（渲染管线、交互清单、字段契约、演示路径）
│   └── screenshots/            实测截图（使用说明书配图）
│
├── requirements.txt            Python 依赖（锁定版本）
├── requirement.txt             同上（课程交付要求的文件名）
├── .env.example                配置模板（复制为 .env 后改密码）
├── .gitignore
├── start.sh                    一键启动脚本
└── README.md                   本文档
```

**三个「为什么这样分」**

1. `app/` 按职责分层（配置 → 数据 → 翻译 → 执行 → 解释），而不是按技术分层。答辩讲代码时，
   顺着「一句话怎么变成中文答案」的链路走一遍即可：`nl2cypher` → `store` → `explain`。
2. `tools/` 与 `scripts/` 分开：`tools` 造数据（离线、幂等、可复现），`scripts` 管数据库和自测（要连环境）。
   改数据只动 `tools/build_dataset.py`，四个数据文件一键重生成，避免手改 CSV 造成不一致。
3. `cypher/` 独立于 Python：老师想直接在 Neo4j Browser 里验数据，不用跑我们的代码。

---

## 6. 常用命令速查

| 目的 | 命令 |
| --- | --- |
| 启动服务 | `./start.sh` 或 `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000` |
| 换端口启动 | `PORT=8010 ./start.sh` |
| 停止服务 | 终端里 `Ctrl+C`；或 `kill $(lsof -ti :8000)` |
| 重建数据集 | `.venv/bin/python tools/build_dataset.py` |
| 数据体检 | `.venv/bin/python tools/validate_dataset.py` |
| 导入预览 | `.venv/bin/python scripts/import_neo4j.py --dry-run` |
| 导入数据库 | `.venv/bin/python scripts/import_neo4j.py --reset` |
| 检查数据库 | `.venv/bin/python scripts/check_connection.py` |
| 接口自测 | `.venv/bin/python scripts/smoke_test_api.py` |
| 安全自测 | `.venv/bin/python scripts/security_selftest.py --base-url http://127.0.0.1:8000` |
| 演示问句自检 | `.venv/bin/python scripts/demo_questions.py --base-url http://127.0.0.1:8000`（12 句含答案断言） |
| 强制只查数据库 | `KG_MODE=neo4j ./start.sh` |

---

## 7. 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/graph` | 全图节点与关系（D3 渲染用），含 `confidence` / `meta` |
| POST | `/api/query` | 自然语言提问，返回 `answer` / `rows` / `cypher` / `params` / `intent` / 命中子图 |
| GET | `/api/query?q=...` | 同上，GET 形式（浏览器地址栏可直接试） |
| GET | `/api/health` | 健康检查：数据源、Neo4j 连接状态、数量 |
| GET | `/api/examples` | 示例问句（前端快捷按钮） |
| GET | `/api/entities?keyword=` | 实体词典检索（搜索联想） |
| GET | `/api/security/report` | 安全护栏拦截统计（防注入演示） |

字段细节见 `docs/03_api_reference.md`。

---

## 8. 常见问题（FAQ）

| 现象 | 原因 | 解决 |
| --- | --- | --- |
| `zsh: command not found: uvicorn` | 没装依赖，或不在项目目录 | `cd` 进项目目录后 `./start.sh`（会自动用 `.venv`） |
| `cd: no such file or directory: 课程设计` / `./start.sh 不存在` | 当前在主目录，没进到项目目录 | 用完整路径：`cd ~/研一/机器学习/课程设计`（`ls` 看到 `start.sh` 才算对了）；或直接双击 `启动系统.command` |
| `[Errno 48] address already in use` | 8000 端口已有服务在跑（通常是好事，说明已启动） | 直接开浏览器；或 `PORT=8010 ./start.sh`；或 `kill $(lsof -ti :8000)` |
| 页面提示「图谱数据加载失败 / Failed to fetch」 | 后端没启动，或端口不是 8000 | 启动服务后刷新；若换了端口，要用新端口访问页面 |
| `No matching Bolt version` / `Cannot decode response` | Neo4j 是 5.x，与 py2neo 2021.2.4 不兼容 | 新建一个 4.4.x DBMS 重新导入 |
| `Cannot open connection to ConnectionProfile('bolt://...')` | DBMS 没启动 / 端口写错 | 确认 DBMS 状态 Running；URI 用 `bolt://localhost:7687` |
| `authentication failure` | 密码与 `.env` 不一致 | 改 `.env` 的 `NEO4J_PASSWORD`，或重设 DBMS 密码 |
| `Couldn't load the external resource at: file:///...` | CSV 不在 import 目录 / 文件名大小写不符 | 拷进 DBMS 的 import 目录，核对 `relations.csv` 拼写 |
| 页面右上角显示「本地图（Neo4j 未连接）」 | 数据库未启动，系统已自动降级 | 功能不受影响；需要绿色徽标就启动 DBMS 后重启服务 |
| 首次访问 Neo4j 要改密码 | 4.4 首次登录默认 `neo4j/neo4j` | 按提示改成 `你的Neo4j密码`，同步 `.env` |
| 中文 CSV 用 Excel 另存后乱码 | Excel 会存成 GBK | 不要用 Excel 另存，直接用仓库里的 CSV |

---

## 9. 课程交付物对照

| 课程要求 | 对应文件 |
| --- | --- |
| 完整可运行源代码 | `app/` `web/` `scripts/` `tools/` `cypher/`（约 5200 行，含中文注释） |
| requirement.txt（全部依赖及版本） | `requirements.txt` / `requirement.txt` |
| README.md 部署文档 | 本文档 |
| 产品使用说明书（图文） | 阶段6 交付：`docs/使用说明书.md` |
| 产品设计思路文档 | 阶段6 交付：`docs/设计思路报告.md`（素材见 `docs/01`、`docs/04`、`docs/05`） |
| 可演示 Demo | `./start.sh` + <http://127.0.0.1:8000/> |
| Schema 设计 + 数据集 | `docs/01_schema_design.md` + `data/entity_relation.csv` |

---

## 10. 文档索引

| 文档 | 内容 |
| --- | --- |
| `docs/01_schema_design.md` | 图模型定义、4 类实体判定规则、5 类关系语义、组合矩阵、大纲覆盖核对表、NL2Cypher 设计 |
| `docs/02_neo4j_setup.md` | 版本矩阵、安装步骤、两条导入路径、9 条错误对照、验收清单 |
| `docs/03_api_reference.md` | 接口字段、8 类意图、错误码、四层安全防护、前端对接建议 |
| `docs/04_optimization_design.md` | Cypher 注入威胁模型与四层防御、置信度标注模型与落地、已知局限 |
| `docs/05_frontend_design.md` | 布局结构、D3 渲染管线、交互清单、字段契约、60 秒演示路径 |
| `docs/使用说明书.md` | ★阶段6 交付：图文产品使用说明书（全部功能 + 操作步骤 + FAQ，7 张实测截图） |
| `docs/设计思路报告.md` | ★阶段6 交付：产品设计思路报告（Vibe Coding：需求分析 / 架构 / 建模 / Agent / 优化） |
| `docs/Demo演示脚本.md` | ★阶段7 交付：现场汇报脚本（逐字台词 + 操作步骤 + Q&A 预案 + 应急预案，含 3 分钟压缩版） |
| `docs/自检报告.md` | 项目自检报告（Schema 双口径核对 / 抽取能力评测 / 批量稳定性测试，复现：`scripts/kg_selfcheck.py`） |

---

## 11. 数据说明

- **实体 113 个**：机器学习概念 38、算法 34、模型 23、评价指标 18（课程要求 80~120）；
- **关系 258 条**：属于 75、衍生自 22、基于 48、可对比 47、应用于 66（仅 5 类，无新增）；
- 全图**单一连通分量**，无孤立节点、无自环、无重复边，平均度 4.55，直径 11，平均最短路径约 3.5 跳；
- 每条关系带置信度（0.98/0.92/0.86/0.78 四档）与依据文本，77 条低置信关系单列清单待复核；
- 数据由 `tools/build_dataset.py` 统一生成，可一键重建，避免手工维护 CSV 不一致。
