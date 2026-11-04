#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
main.py
--------
FastAPI 应用入口：对外提供两个核心接口 + 两个辅助接口。

核心接口：
    GET  /api/graph          全量图谱数据（节点 + 关系），供 D3 力导向图渲染
    POST /api/query          自然语言问答：NL → Cypher → Neo4j → 结果 + 中文解释
    GET  /api/query?q=...    同一个问答接口的 GET 版，方便浏览器里直接敲 URL 演示

辅助接口：
    GET  /api/health         健康检查：Neo4j 是否连上、节点/关系数量、当前数据源
    GET  /api/examples       示例问句（前端做快捷按钮）
    GET  /api/security/report 安全护栏拦截报告（演示"防注入"用）

启动：
    uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
    或  python -m app.main
文档：
    http://127.0.0.1:8000/docs
"""

import re
import time
from dataclasses import replace
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ROOT, settings
from .explain import explain, fragment
from .kgdata import RELATION_TYPES, TYPE_COLORS, kb
from .nl2cypher import EXAMPLES, build_cypher, build_plan, link_entities
from .security import SecurityError, guard
from .store import neo4j_store, pick_store

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="机器学习领域知识图谱系统 —— 后端 API（Neo4j + py2neo + FastAPI）",
)

# 阶段4 前端可能是 file:// 打开或跑在另一个端口，统一放开跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 一、请求/响应模型（pydantic 会自动做参数校验，并在 /docs 里生成表单）
# ---------------------------------------------------------------------------
class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=settings.MAX_QUESTION_LEN,
                          description="自然语言问句", examples=["随机森林属于哪一类算法？"])
    limit: int = Field(settings.DEFAULT_LIMIT, ge=1, le=settings.MAX_LIMIT,
                       description="单次返回条数上限")


class QueryResponse(BaseModel):
    question: str
    intent: str                     # 查询意图（detail/neighbors/filter/...）
    rule: str                       # 命中的解析规则，便于答辩时解释"为什么这么翻译"
    cypher: str                     # 实际执行的 Cypher
    params: dict                    # Cypher 参数
    answer: str                     # 自然语言解释
    rows: list                      # 原始查询结果
    direct: list = []               # 仅 compare：两实体之间的直接关系
    path: dict | None = None        # 仅 path/compare：最短路径
    nodes: list = []                # 命中子图节点（前端高亮用）
    links: list = []                # 命中子图边
    matched_entities: list = []     # 识别到的实体
    source: str = "neo4j"           # 实际数据源：neo4j / local
    degraded: bool = False          # 是否降级到本地图
    relaxed: bool = False            # 是否因原类型无结果而放开了类型限制
    warning: str | None = None
    elapsed_ms: float = 0.0


# ---------------------------------------------------------------------------
# 二、安全护栏：只读校验 + 注入防护（实现见 app/security.py）
# ---------------------------------------------------------------------------
def safe_enforce(cypher: str, params: dict | None = None) -> None:
    """
    调用安全护栏；把内部异常翻译成 HTTP 500。
    说明：能走到这里说明是**我们自己模板**出了问题（用户输入从不拼进语句），
    所以对用户统一返回"语句未通过安全检查"，具体原因已写入 logs/security.log。
    """
    try:
        guard.enforce(cypher, params)
    except SecurityError as exc:
        raise HTTPException(status_code=500, detail=exc.message) from exc


# ---------------------------------------------------------------------------
# 三、/api/graph —— 全量图谱
# ---------------------------------------------------------------------------
_graph_cache: dict = {"ts": 0.0, "data": None, "source": None}


@app.get("/api/graph", summary="获取全部图谱节点与关系（供 D3 渲染）")
def get_graph(refresh: bool = Query(False, description="true 表示跳过缓存，强制重查")):
    """
    返回结构固定为 {nodes, links, meta}，字段说明见 docs/03_api_reference.md。

    节点用实体名作为 id：一个是前端 D3 里读起来直观，另一个是 links 里
    source/target 直接写名字即可，不必再维护一份 id 映射表。
    """
    now = time.time()
    if (not refresh and _graph_cache["data"] is not None
            and now - _graph_cache["ts"] < settings.GRAPH_CACHE_TTL):
        return _graph_cache["data"]

    store, source, degraded = pick_store()
    try:
        data = store.graph_data()
    except ConnectionError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"图数据库不可用：{exc}。可先启动 Neo4j，或设置 KG_MODE=local 用本地数据演示。",
        )

    data["meta"] = {
        **kb.counts(),
        "type_colors": TYPE_COLORS,
        "relation_types": RELATION_TYPES,
        # 置信度模型（4 档 + 复核阈值）随图一起下发，前端图例直接读，不在前端写死
        "confidence_model": kb.meta.get("confidence_model"),
        "source": source,
        "degraded": degraded,
    }
    _graph_cache.update({"ts": now, "data": data, "source": source})
    return data


# ---------------------------------------------------------------------------
# 四、/api/query —— 自然语言问答
# ---------------------------------------------------------------------------
def _do_query(question: str, limit: int) -> QueryResponse:
    question = (question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="问句不能为空")
    if len(question) > settings.MAX_QUESTION_LEN:
        raise HTTPException(status_code=400, detail=f"问句过长（上限 {settings.MAX_QUESTION_LEN} 字）")

    # 安全护栏 L1：问句里若混入 Cypher 片段（MATCH/DELETE/; / $ 等）直接拒绝。
    # 本项目的翻译层本来就不拼用户输入，这一层的作用是"显式拒绝 + 留痕"，避免被当成注入入口。
    try:
        guard.screen_question(question)
    except SecurityError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    started = time.perf_counter()

    # Step 1-3：实体识别 → 意图分类 → 模板填充
    plan = build_plan(question, limit=limit)
    cypher, params = build_cypher(plan)
    safe_enforce(cypher, params)

    # Step 4：执行（Neo4j 优先，auto 模式下失败自动降级本地图）
    store, source, degraded = pick_store()
    try:
        result = store.query(plan)

        # 类型筛选零结果时自动放宽：图谱里没有"模型属于深度学习"，但可能有算法，
        # 与其回一句"没找到"，不如把同义结果给出来并说明类型已放开。
        if plan.kind == "filter" and not result.get("rows") and plan.entity_type:
            relaxed_plan = replace(plan, entity_type=None, relaxed_from=plan.entity_type)
            relaxed_result = store.query(relaxed_plan)
            if relaxed_result.get("rows"):
                plan = relaxed_plan
                result = relaxed_result
                cypher, params = build_cypher(plan)
                safe_enforce(cypher, params)
    except Exception as exc:
        if settings.KG_MODE == "neo4j":
            raise HTTPException(
                status_code=503,
                detail=f"Neo4j 查询失败：{exc}。请确认数据库已启动、密码正确（见 docs/02_neo4j_setup.md）。",
            )
        # auto/local 模式下本地图理论上不会失败，兜底给出可读信息
        raise HTTPException(status_code=500, detail=f"查询执行失败：{exc}")

    # Step 5：结果 → 中文解释 + 高亮子图
    answer = explain(plan, result, source)
    frag = fragment(plan, result)

    warning = None
    if degraded and settings.KG_MODE == "auto":
        warning = ("Neo4j 未连接，当前用本地 kg.json 计算（查询逻辑与数据库一致）。"
                   "启动 Neo4j 后重新提问即可走真实图查询。")

    return QueryResponse(
        question=question,
        intent=plan.kind,
        rule=plan.rule,
        cypher=cypher,
        params=params,
        answer=answer,
        rows=result.get("rows", []),
        direct=result.get("direct", []),
        path=result.get("path"),
        nodes=frag["nodes"],
        links=frag["links"],
        matched_entities=[{"name": e["name"], "type": e["type"], "id": e["id"]} for e in plan.entities],
        source=source,
        degraded=degraded,
        relaxed=bool(plan.relaxed_from),
        warning=warning,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
    )


@app.post("/api/query", response_model=QueryResponse, summary="自然语言问答（NL→Cypher）")
def query_post(body: QueryRequest):
    """
    收到中文问句 → 规则解析成 QueryPlan → 生成参数化 Cypher → 查 Neo4j
    → 返回查询结果 + 中文解释 + 命中子图。
    """
    return _do_query(body.question, body.limit)


@app.get("/api/query", response_model=QueryResponse, summary="自然语言问答（GET 版，便于浏览器直接测试）")
def query_get(
    q: str = Query(..., description="自然语言问句", examples=["随机森林和梯度提升树有什么区别？"]),
    limit: int = Query(settings.DEFAULT_LIMIT, ge=1, le=settings.MAX_LIMIT),
):
    return _do_query(q, limit)


# ---------------------------------------------------------------------------
# 五、辅助接口
# ---------------------------------------------------------------------------
@app.get("/api/health", summary="健康检查：数据库连接与数据规模")
def health():
    """答辩现场先跑这个，一眼看出数据库有没有起来、数据对不对。"""
    neo4j_ok = neo4j_store.connect(force=True)
    dataset = kb.counts()
    info = {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "kg_mode": settings.KG_MODE,
        "uri": settings.NEO4J_URI if settings.neo4j_configured else None,
        # store 说明这一份数据是谁在执行：neo4j = Cypher 打到图库；kg.json = 服务端数据集
        "store": "neo4j" if neo4j_ok else "kg.json",
        "dataset_source": "data/kg.json（服务端读取，随部署一起分发）",
        "neo4j": {
            "connected": neo4j_ok,
            "configured": settings.neo4j_configured,
            "error": None if neo4j_ok else neo4j_store.last_error,
        },
        "local_dataset": dataset,
    }
    if neo4j_ok:
        try:
            nodes = neo4j_store.run("MATCH (n:Entity) RETURN count(n) AS c")[0]["c"]
            rels = neo4j_store.run("MATCH ()-[r]->() RETURN count(r) AS c")[0]["c"]
            info["neo4j"].update({"nodes": nodes, "relationships": rels})
            if (nodes, rels) != (dataset["entities"], dataset["relations"]):
                info["warning"] = "数据库数量与本地数据集不一致，建议重跑 python scripts/import_neo4j.py --reset"
        except Exception as exc:
            info["neo4j"]["error"] = str(exc)
    return info


@app.get("/api/examples", summary="示例问句（前端快捷按钮）")
def examples():
    return {"examples": EXAMPLES}


@app.get("/api/entities", summary="实体词典（名称/别名/类型，前端搜索框联想用）")
def entities(keyword: str = "", limit: int = Query(20, ge=1, le=200)):
    if keyword:
        items = kb.search(keyword, limit=limit)
    else:
        items = kb.entities[:limit]
    return {
        "total": len(kb.entities),
        "items": [
            {"id": e["id"], "name": e["name"], "type": e["type"],
             "aliases": e.get("aliases", ""), "description": e.get("description", "")}
            for e in items
        ],
    }


@app.get("/api/security/report", summary="安全护栏拦截报告（防注入演示用）")
def security_report():
    """
    返回护栏的实时统计：校验了多少条语句、拦截了多少、各原因分布、最近样本。
    现场演示：先在 /api/query 里发一条带 MATCH ... DELETE 的"问句"，
    再打开这个接口，就能看到拦截记录 —— 比口头说"我们做了防护"有说服力。
    """
    return guard.report()


# ---------------------------------------------------------------------------
# 六、静态页面（阶段4 的 web/ 目录完成后自动挂载到根路径）
# ---------------------------------------------------------------------------
WEB_DIR = ROOT / "web"
if WEB_DIR.exists() and (WEB_DIR / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
else:

    @app.get("/", include_in_schema=False)
    def root():
        """前端还没做时，访问根路径直接跳到接口文档，避免看到 404。"""
        return RedirectResponse(url="/docs")


if __name__ == "__main__":
    import uvicorn

    # reload=False：正式演示用；开发时用命令行加 --reload
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=False)
