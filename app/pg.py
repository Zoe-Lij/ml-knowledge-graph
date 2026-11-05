#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pg.py
------
PostgreSQL 数据访问层（线上部署用）。

为什么要有这一层？
    Vercel 的 Serverless 函数进程随时会被回收，**不能**在函数里跑一个常驻数据库；
    本机 Neo4j 也带不到云上（Aura 免费档在中国区注册被拦）。
    所以线上改成"托管 PostgreSQL（Neon / Supabase / 云数据库都行）+ 纯 SQL 查询"，
    数据存在数据库里，换电脑、刷新页面、重新部署都不丢。

表结构（见 scripts/init_postgres.py）：
    kg_entity(id, name, type, en_name, aliases, description, confidence, source)
    kg_relation(id, source_name, target_name, rel_type, description,
                symmetric, confidence, evidence)
    kg_meta(key, value)          -- 数据集元信息（版本、置信度模型等）

查询怎么实现？
    Neo4j 用 Cypher，PostgreSQL 用 SQL，两者的"查询计划"是同一份 QueryPlan。
    本模块把 QueryPlan 编译成参数化 SQL：
        · 一跳邻居 / 条件筛选 / 类型列表 / 统计 / 检索 —— 普通 JOIN + WHERE
        · 多跳最短路径 —— WITH RECURSIVE 递归 CTE，在数据库内部做 BFS
    所有用户输入都走 %(name)s 占位符，不做任何字符串拼接，天然免疫 SQL 注入。

兼容性说明（部署踩坑记录）：
    · Neon 的 pooled 连接串走 PgBouncer（transaction 模式），
      服务端预处理语句会报 "prepared statement already exists"，
      因此连接时设置 prepare_threshold=None 关闭自动 prepare。
    · 云数据库必须 SSL：连接串里带 ?sslmode=require 即可（Neon 默认给的就是这个）。
"""

import threading
import time

from .config import settings

# 关系类型白名单（与 Schema 严格一致，任何地方出现第 6 种都是 bug）
DEFAULT_RELS = ["属于", "衍生自", "基于", "可对比", "应用于"]
MAX_HOPS = 6

# 多跳最短路径：一条 SQL 同时被 build_sql() 和 _shortest_path() 使用，避免两份 SQL 跑偏。
# 语义对齐 Cypher 的 allShortestPaths：
#   · 无向遍历（source 或 target 命中当前节点都算一条边）；
#   · NOT (... = ANY(names)) 做环检测，走过的节点不再走；
#   · 到达终点就不再往下扩（b.node <> 终点），避免全图铺开；
#   · 只保留"最短"的那一层（depth = min(depth)），等价于 allShortestPaths；
#   · 每跳同时带 start/end 名字，解释层按真实方向画箭头。
PATH_SQL = """
WITH RECURSIVE bfs(node, names, rels, descs, confs, srcs, tgts, depth) AS (
    SELECT %(a0)s::text AS node,
           ARRAY[%(a)s::text] AS names,
           ARRAY[]::text[] AS rels,
           ARRAY[]::text[] AS descs,
           ARRAY[]::float8[] AS confs,
           ARRAY[]::text[] AS srcs,
           ARRAY[]::text[] AS tgts,
           0 AS depth
    UNION ALL
    SELECT nb.nxt, b.names || nb.nxt, b.rels || nb.rel, b.descs || nb.descr,
           b.confs || nb.conf, b.srcs || nb.src, b.tgts || nb.tgt, b.depth + 1
    FROM bfs b
    CROSS JOIN LATERAL (
        SELECT CASE WHEN r.source_name = b.node THEN r.target_name ELSE r.source_name END AS nxt,
               r.rel_type AS rel,
               COALESCE(r.description, '') AS descr,
               COALESCE(r.confidence, 0.90)::float8 AS conf,
               r.source_name AS src,
               r.target_name AS tgt
        FROM kg_relation r
        WHERE (r.source_name = b.node OR r.target_name = b.node)
          AND NOT ((CASE WHEN r.source_name = b.node THEN r.target_name ELSE r.source_name END)
                   = ANY(b.names))
    ) nb
    WHERE b.depth < %(max_hops)s AND b.node <> %(b)s
), hit AS (
    SELECT * FROM bfs WHERE node = %(b)s
), best AS (
    SELECT min(depth) AS d FROM hit
)
SELECT depth AS hops,
       names AS path_names,
       rels AS path_rels,
       descs AS path_descs,
       confs AS path_confs,
       srcs AS path_srcs,
       tgts AS path_tgts
FROM hit, best
WHERE hit.depth = best.d
ORDER BY hit.names
LIMIT 5
"""


# ---------------------------------------------------------------------------
# 一、连接管理
# ---------------------------------------------------------------------------
def _dsn() -> str:
    return settings.database_url


def _connect():
    """新建一条连接。psycopg 用 dict_row，取出来就是 dict，和 Neo4j 路径字段对齐。"""
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(
        _dsn(),
        autocommit=True,                 # 只读查询，不需要显式事务
        row_factory=dict_row,
        connect_timeout=settings.PG_CONNECT_TIMEOUT,
        prepare_threshold=None,          # 兼容 PgBouncer / Neon pooler（见文件头说明）
        application_name="ml-kg",
    )


class PgConnection:
    """
    一条可复用的连接。

    为什么要复用？Serverless 每次冷启动都重建 TLS 连接要 200~400ms，
    同一个容器内的后续请求复用同一条连接可以省掉这部分开销。
    连接断了（云实例空闲回收）就自动重连一次，对上层透明。
    """

    def __init__(self):
        self._conn = None
        self._lock = threading.Lock()

    @property
    def alive(self) -> bool:
        return self._conn is not None and not self._conn.closed

    def _open(self):
        self._close()
        self._conn = _connect()

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def close(self) -> None:
        with self._lock:
            self._close()

    def execute(self, sql: str, params: dict | None = None) -> list[dict]:
        """执行一条 SQL，返回 dict 列表；连接失效时自动重连并重试一次。"""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if not self.alive:
                        self._open()
                    cur = self._conn.execute(sql, params or {})
                    return [dict(r) for r in cur.fetchall()]
                except Exception:
                    self._close()
                    if attempt == 2:
                        raise
        return []      # 理论上到不了这里

    def ping(self) -> bool:
        try:
            self.execute("SELECT 1 AS ok")
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# 二、QueryPlan → SQL
# ---------------------------------------------------------------------------
def _rels(plan) -> list[str]:
    return list(plan.relations or DEFAULT_RELS)


def build_sql(plan) -> tuple[str, dict]:
    """
    把 QueryPlan 编译成 (sql, params)。
    与 nl2cypher.build_cypher 一一对应：同样的意图、同样的返回字段，
    这样 explain.py / 前端都不用区分底层是 Neo4j 还是 PostgreSQL。
    """
    limit = max(1, min(plan.limit, settings.MAX_LIMIT))

    # ---------- 一跳邻居 / 实体详情 ----------
    if plan.kind in ("neighbors", "detail"):
        sql = """
SELECT r.rel_type AS relation,
       CASE WHEN r.source_name = %(name)s THEN 'out' ELSE 'in' END AS direction,
       CASE WHEN r.source_name = %(name)s THEN r.target_name ELSE r.source_name END AS name,
       COALESCE(m.type, '') AS type,
       COALESCE(r.description, '') AS description,
       COALESCE(r.confidence, 0.90)::float8 AS confidence
FROM kg_relation r
LEFT JOIN kg_entity m
       ON m.name = CASE WHEN r.source_name = %(name)s THEN r.target_name ELSE r.source_name END
WHERE (r.source_name = %(name)s OR r.target_name = %(name)s)
  AND r.rel_type = ANY(%(rels)s)
  AND COALESCE(m.name, '') <> %(name)s
  AND (%(direction)s::text IS NULL
       OR CASE WHEN r.source_name = %(name)s THEN 'out' ELSE 'in' END = %(direction)s)
ORDER BY relation, name
LIMIT %(limit)s
"""
        return sql.strip(), {"name": plan.entities[0]["name"], "direction": plan.direction,
                             "rels": _rels(plan), "limit": limit}

    # ---------- 条件筛选：候选 -[关系]-> 目标 ----------
    if plan.kind == "filter":
        sql = """
SELECT e.name AS name, e.type AS type, COALESCE(e.description, '') AS description,
       r.rel_type AS relation, COALESCE(r.description, '') AS rel_description,
       COALESCE(r.confidence, 0.90)::float8 AS confidence,
       COALESCE(r.evidence, '') AS evidence,
       r.target_name AS target
FROM kg_relation r
JOIN kg_entity e ON e.name = r.source_name
WHERE r.target_name = %(target)s
  AND r.rel_type = ANY(%(rels)s)
  AND (%(etype)s::text IS NULL OR e.type = %(etype)s)
ORDER BY e.name
LIMIT %(limit)s
"""
        return sql.strip(), {"target": plan.target, "rels": _rels(plan),
                             "etype": plan.entity_type, "limit": limit}

    # ---------- 按类型列表 ----------
    if plan.kind == "type_list":
        sql = """
SELECT name, type, COALESCE(description, '') AS description
FROM kg_entity
WHERE (%(etype)s::text IS NULL OR type = %(etype)s)
ORDER BY name
LIMIT %(limit)s
"""
        return sql.strip(), {"etype": plan.entity_type, "limit": limit}

    # ---------- 双实体对比：共同邻居 ----------
    if plan.kind == "compare":
        sql = """
WITH a_nb AS (
    SELECT CASE WHEN r.source_name = %(a)s THEN r.target_name ELSE r.source_name END AS nb,
           r.rel_type
    FROM kg_relation r
    WHERE %(a)s IN (r.source_name, r.target_name)
), b_nb AS (
    SELECT CASE WHEN r.source_name = %(b)s THEN r.target_name ELSE r.source_name END AS nb,
           r.rel_type
    FROM kg_relation r
    WHERE %(b)s IN (r.source_name, r.target_name)
)
SELECT e.name AS name, e.type AS type,
       min(a.rel_type) AS rel_a, min(b.rel_type) AS rel_b,
       COALESCE(e.description, '') AS description
FROM a_nb a
JOIN b_nb b ON a.nb = b.nb
JOIN kg_entity e ON e.name = a.nb
WHERE a.nb <> %(a)s AND a.nb <> %(b)s
GROUP BY e.name, e.type, e.description
ORDER BY e.name
LIMIT %(limit)s
"""
        a, b = plan.entities[0]["name"], plan.entities[1]["name"]
        return sql.strip(), {"a": a, "b": b, "limit": limit}

    # ---------- 多跳最短路径（递归 CTE 做 BFS）----------
    if plan.kind == "path":
        a, b = plan.entities[0]["name"], plan.entities[1]["name"]
        return PATH_SQL.strip(), {"a": a, "a0": a, "b": b,
                                  "max_hops": min(MAX_HOPS, settings.MAX_HOPS)}

    # ---------- 度数统计 ----------
    if plan.kind == "stats":
        sql = """
SELECT e.name AS name, e.type AS type, count(r.id) AS degree
FROM kg_entity e
LEFT JOIN kg_relation r ON e.name IN (r.source_name, r.target_name)
WHERE (%(etype)s::text IS NULL OR e.type = %(etype)s)
GROUP BY e.name, e.type
ORDER BY degree DESC, e.name
LIMIT %(limit)s
"""
        return sql.strip(), {"etype": plan.entity_type, "limit": limit}

    # ---------- 关键词检索（search / not_found 兜底）----------
    sql = """
SELECT name, type, COALESCE(description, '') AS description
FROM kg_entity
WHERE name ILIKE %(kw)s
   OR COALESCE(aliases, '') ILIKE %(kw)s
   OR COALESCE(en_name, '') ILIKE %(kw)s
ORDER BY name
LIMIT %(limit)s
"""
    kw = f"%{(plan.keyword or '').strip()}%"
    return sql.strip(), {"kw": kw, "limit": limit}


# ---------------------------------------------------------------------------
# 三、PostgreSQL 后端
# ---------------------------------------------------------------------------
class PostgresStore:
    """与 Neo4jStore / LocalStore 同接口（query + graph_data），可被 main.py 直接替换。"""

    engine = "postgres"

    def __init__(self):
        self._conn = PgConnection()
        self.last_error = ""
        self._last_fail_at = 0.0
        self._retry_interval = 5.0

    # -- 连接 ----------------------------------------------------------
    def connect(self, force: bool = False) -> bool:
        if not settings.database_configured:
            self.last_error = "未配置 DATABASE_URL（当前数据源：服务端 data/kg.json）"
            return False
        if self._conn.alive:
            if not force:
                return True
            if self._conn.ping():
                return True
            self._conn.close()
        if not force and (time.time() - self._last_fail_at) < self._retry_interval:
            return False
        try:
            self._conn.execute("SELECT 1 AS ok")
            self.last_error = ""
            return True
        except Exception as exc:          # 连不上不抛给上层，交给降级逻辑
            self._last_fail_at = time.time()
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    @property
    def connected(self) -> bool:
        return self._conn.alive

    def close(self) -> None:
        self._conn.close()

    # -- 执行 ----------------------------------------------------------
    def run(self, sql: str, params: dict | None = None) -> list[dict]:
        if not self.connect():
            raise ConnectionError(self.last_error or "PostgreSQL 未连接")
        try:
            return self._conn.execute(sql, params)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise

    def query(self, plan) -> dict:
        sql, params = build_sql(plan)
        result: dict = {"rows": [], "direct": [], "path": None,
                        "sql": sql, "params": params, "cypher": None}

        if plan.kind == "compare":
            a, b = plan.entities[0]["name"], plan.entities[1]["name"]
            result["direct"] = self._direct_lookup(a, b)
            result["path"] = self._shortest_path(a, b)
            result["rows"] = self.run(sql, params)
            return result

        if plan.kind == "path":
            a, b = plan.entities[0]["name"], plan.entities[1]["name"]
            # 直接边优先：两个实体之间有边就返回它，不强行凑多跳链
            direct = self._direct_lookup(a, b)
            if direct:
                result["direct"] = direct
                result["path"] = None
                result["rows"] = []
                return result
            path = self._shortest_path(a, b)
            result["path"] = path
            result["rows"] = ([{"hops": path["hops"], "path_names": path["names"],
                                "path_rels": path["rels"], "path_descs": path["descs"],
                                "path_confs": path["confs"], "path_srcs": path.get("srcs", []),
                                "path_tgts": path.get("tgts", [])}] if path else [])
            return result

        result["rows"] = self.run(sql, params)
        return result

    # -- 图的特殊查询 --------------------------------------------------
    def _direct_lookup(self, a: str, b: str) -> list[dict]:
        """查 a、b 之间的直接边（无向），供 path 意图"直接边优先"规则使用。"""
        return self.run(
            """
SELECT r.rel_type AS relation, COALESCE(r.description, '') AS description,
       COALESCE(r.confidence, 0.90)::float8 AS confidence, COALESCE(r.evidence, '') AS evidence
FROM kg_relation r
WHERE (r.source_name = %(a)s AND r.target_name = %(b)s)
   OR (r.source_name = %(b)s AND r.target_name = %(a)s)
ORDER BY confidence DESC
""",
            {"a": a, "b": b},
        )

    def _shortest_path(self, a: str, b: str) -> dict | None:
        """无向最短路径，返回全部等长候选后由 _pick_path 择优（与 Neo4j 路径一致）。"""
        from .store import _pick_path      # 复用同一套择优规则，避免两套逻辑跑偏

        rows = self.run(PATH_SQL.strip(), {"a": a, "a0": a, "b": b,
                                           "max_hops": min(MAX_HOPS, settings.MAX_HOPS)})
        return _pick_path([self._row_to_path(r) for r in rows])

    @staticmethod
    def _row_to_path(row: dict) -> dict:
        return {
            "hops": row["hops"],
            "names": row["path_names"],
            "rels": row["path_rels"],
            "descs": row["path_descs"],
            "confs": row["path_confs"],
            "srcs": row["path_srcs"],
            "tgts": row["path_tgts"],
        }

    # -- 整图与统计 ----------------------------------------------------
    def graph_data(self) -> dict:
        from .kgdata import TYPE_COLORS, TYPE_TO_LABEL

        nodes = self.run(
            """
SELECT e.name AS id, e.id AS eid, e.type, e.en_name, e.aliases, e.description,
       e.confidence::float8 AS confidence, e.source,
       (SELECT count(*) FROM kg_relation r
         WHERE r.source_name = e.name OR r.target_name = e.name) AS degree
FROM kg_entity e
ORDER BY e.type, e.name
"""
        )
        links = self.run(
            """
SELECT source_name AS source, target_name AS target, rel_type AS type,
       COALESCE(description, '') AS description, COALESCE(is_symmetric, false) AS symmetric,
       COALESCE(confidence, 0.90)::float8 AS confidence, COALESCE(evidence, '') AS evidence
FROM kg_relation
ORDER BY id
"""
        )
        for n in nodes:
            n["label"] = TYPE_TO_LABEL.get(n.get("type"), "Entity")
            n["color"] = TYPE_COLORS.get(n.get("type"), "#999999")
            n["degree"] = int(n.get("degree") or 0)
            n["confidence"] = float(n.get("confidence") or 1.0)
        for l in links:
            l["confidence"] = float(l.get("confidence") or 0.90)
            l["symmetric"] = bool(l.get("symmetric"))
        return {"nodes": nodes, "links": links}

    def counts(self) -> dict:
        """按类型/关系类型统计（/api/health 与前端图例用）。"""
        rows = self.run("SELECT type, count(*)::int AS c FROM kg_entity GROUP BY type")
        by_type = {r["type"]: r["c"] for r in rows}
        rows = self.run("SELECT rel_type, count(*)::int AS c FROM kg_relation GROUP BY rel_type")
        by_rel = {r["rel_type"]: r["c"] for r in rows}
        return {
            "entities": sum(by_type.values()),
            "relations": sum(by_rel.values()),
            "by_type": by_type,
            "by_relation": by_rel,
        }


# ---------------------------------------------------------------------------
# 四、启动引导：让实体词典也来自数据库
# ---------------------------------------------------------------------------
def fetch_dataset() -> dict:
    """
    从数据库读回整份数据集，拼成与 data/kg.json 完全相同的结构。

    用途：kgdata.KnowledgeBase 用它构造实体词典（NL→语句 的实体识别依赖这份词典），
    以及 /api/graph 的 meta。这样运行期的数据来源就只有数据库一个，
    kg.json 退化成"初始化种子 + 数据库不通时的兜底"。
    """
    entities = _conn_for_bootstrap().execute(
        """
SELECT id, name, type, en_name, aliases, description, confidence::float8 AS confidence, source
FROM kg_entity ORDER BY id
"""
    )
    rows = _conn_for_bootstrap().execute(
        """
SELECT source_name, target_name, rel_type, description, is_symmetric,
       confidence::float8 AS confidence, evidence
FROM kg_relation ORDER BY id
"""
    )
    meta_rows = _conn_for_bootstrap().execute("SELECT key, value FROM kg_meta")

    name_to_id = {e["name"]: e["id"] for e in entities}
    relations = []
    for i, r in enumerate(rows, start=1):
        relations.append({
            "id": f"R{i:03d}",
            "source": name_to_id.get(r["source_name"], r["source_name"]),
            "target": name_to_id.get(r["target_name"], r["target_name"]),
            "type": r["rel_type"],
            "symmetric": bool(r["is_symmetric"]),
            "description": r["description"] or "",
            "confidence": float(r["confidence"] if r["confidence"] is not None else 0.90),
            "evidence": r["evidence"] or "",
        })

    meta: dict = {}
    for m in meta_rows:
        meta[m["key"]] = m["value"]

    return {"meta": meta, "entities": entities, "relations": relations}


_bootstrap_conn: PgConnection | None = None


def _conn_for_bootstrap() -> PgConnection:
    """引导阶段用一条独立的短连接，不与请求链路抢锁。"""
    global _bootstrap_conn
    if _bootstrap_conn is None or not _bootstrap_conn.alive:
        _bootstrap_conn = PgConnection()
    return _bootstrap_conn
