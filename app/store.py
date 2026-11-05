#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
store.py
---------
数据访问层：同一套 QueryPlan，两种执行后端。

    Neo4jStore —— 把 plan 编译成 Cypher 交给 py2neo 执行（正式路径）
    LocalStore —— 直接在内存的 kg.json 上算（降级路径 / 结果对照）

为什么保留两条路径？
    1. 开发机没装 Neo4j 时，仍能完整跑通"问句 → 答案"的链路，不阻塞阶段4；
    2. 现场演示如果数据库没起来，KG_MODE=auto 会自动降级，不至于开天窗；
    3. 两条实现互为对照，可以验证 Cypher 写得对不对（答辩时可以主动提这点）。

注意：两条路径返回的字段名必须完全一致，否则 explain.py 会拿不到数据。
"""

import time
from collections import deque

from .config import settings
from .kgdata import TYPE_COLORS, TYPE_TO_LABEL, kb
from .nl2cypher import QueryPlan, build_cypher
from .security import guard

# 「属于」是严格有向的分类关系：子类 → 父类（小概念指向大概念）。
# 路径搜索是无向的，如果某条候选链把「属于」边反着走（父类→子类），
# 这条链就违背领域常识，择优时优先剔除。
TAXONOMY_REL = "属于"


def _reversed_taxonomy_hops(path: dict) -> int:
    """统计路径里「属于」边被反向遍历的跳数。"""
    srcs, names = path.get("srcs") or [], path.get("names") or []
    if not srcs:
        return 0
    return sum(1 for i, rel in enumerate(path.get("rels", []))
               if rel == TAXONOMY_REL and i < len(srcs) and srcs[i] != names[i])


def _pick_path(candidates: list[dict]) -> dict | None:
    """
    从等长最短路径候选里选一条最靠谱的：
      1. 没有反向「属于」跳的优先；
      2. 最小置信度高的优先（优先用高置信边，不强行凑长链条）；
      3. 跳数少的优先。
    """
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: (
        _reversed_taxonomy_hops(p),
        -min([float(c) for c in (p.get("confs") or [1])] or [1]),
        p.get("hops", 99),
    ))[0]


# ---------------------------------------------------------------------------
# 一、Neo4j 后端
# ---------------------------------------------------------------------------
class _OfficialDriverConnection:
    """
    把官方 neo4j 驱动包装成与 py2neo Graph 相同的最小接口（只用到 run）。

    为什么需要它？
        本机演示用的是 Neo4j 4.4 + py2neo 2021.2.4（旧版 Bolt 协议）。
        线上（Vercel）连的是 Neo4j Aura 云实例，服务端是 5.x，只认新版 Bolt，
        py2neo 2021.2.4 会在握手阶段直接失败（No matching Bolt version）。
        官方驱动 5.x 向下兼容 4.4 服务端，所以两种环境可以共用同一份 Neo4jStore，
        只是"谁去连"不一样 —— URI 是 neo4j+s:// 之类的云协议时自动切到官方驱动。
    """

    def __init__(self, uri: str, auth: tuple[str, str], database: str):
        from neo4j import GraphDatabase   # 延迟导入：本地没装也不影响 py2neo 路径

        self._database = database
        self._driver = GraphDatabase.driver(
            uri,
            auth=auth,
            max_connection_lifetime=300,     # 云实例会主动断开空闲连接，5 分钟重建一次
            connection_timeout=8 if not settings.is_serverless else 5,
            max_connection_pool_size=8,
        )

    def run(self, cypher: str, params: dict | None = None) -> list[dict]:
        records, _summary, _keys = self._driver.execute_query(
            cypher, parameters_=params or {}, database_=self._database
        )
        return [r.data() for r in records]

    def close(self) -> None:
        try:
            self._driver.close()
        except Exception:
            pass


# 这些 URI 前缀意味着"连的是 5.x 云实例"，必须用官方驱动
_OFFICIAL_DRIVER_SCHEMES = (
    "neo4j://", "neo4j+s://", "neo4j+ssc://",
    "bolt+s://", "bolt+ssc://",
)


def _need_official_driver(uri: str) -> bool:
    # 手动指定优先：本机想验证云端驱动路径时，设 NEO4J_DRIVER=official 即可
    if settings.NEO4J_DRIVER == "official":
        return True
    if settings.NEO4J_DRIVER == "py2neo":
        return False
    return uri.strip().lower().startswith(_OFFICIAL_DRIVER_SCHEMES)


class Neo4jStore:
    """Neo4j 连接管理 + Cypher 执行（本机 py2neo / 云端官方驱动，自动切换）。"""

    def __init__(self):
        self._graph = None
        self.last_error = ""
        self._last_fail_at = 0.0
        self._retry_interval = 5.0      # 连不上时 5 秒内不重复重试，避免每个请求都卡 3 秒

    # -- 连接 ----------------------------------------------------------
    def connect(self, force: bool = False) -> bool:
        # 没有配置任何数据库凭据（典型场景：Vercel 上不配 NEO4J_* 变量）：
        # 直接判定为不可用，绝不尝试连 localhost —— 否则每个请求都要等一次超时。
        if not settings.neo4j_configured:
            self.last_error = "未配置 NEO4J_URI / NEO4J_PASSWORD（当前数据源：服务端 data/kg.json）"
            self._graph = None
            return False
        # 已连上：force 时只发一次心跳确认（云实例空闲会被回收，必须验活而不是重建）
        if self._graph is not None:
            if not force:
                return True
            try:
                self._graph.run("RETURN 1 AS ok")
                return True
            except Exception:
                self._close_quietly()
        # 刚失败过就先别重试，直接返回
        if not force and (time.time() - self._last_fail_at) < self._retry_interval:
            return False
        try:
            if _need_official_driver(settings.NEO4J_URI):
                g = _OfficialDriverConnection(
                    settings.NEO4J_URI,
                    (settings.NEO4J_USER, settings.NEO4J_PASSWORD),
                    settings.NEO4J_DATABASE,
                )
            else:
                from py2neo import Graph

                g = Graph(settings.NEO4J_URI,
                          auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD))
            g.run("RETURN 1 AS ok")     # 构造连接是懒的，必须真发一次请求才算连上
            self._graph = g
            self.last_error = ""
            return True
        except Exception as exc:        # 连接失败不抛给上层，交给降级逻辑
            self._graph = None
            self._last_fail_at = time.time()
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    def _close_quietly(self) -> None:
        if self._graph is not None and hasattr(self._graph, "close"):
            self._graph.close()
        self._graph = None

    @property
    def connected(self) -> bool:
        return self._graph is not None

    # -- 执行 ----------------------------------------------------------
    def run(self, cypher: str, params: dict | None = None) -> list[dict]:
        """
        执行只读 Cypher。
        安全校验做了两次（main.py 生成语句后一次，这里下发数据库前再一次）：
        以后若有人新增了绕过 main.py 的调用点，也拦得住。校验失败抛 SecurityError。
        """
        guard.enforce(cypher, params)
        if not self.connect():
            raise ConnectionError(self.last_error or "Neo4j 未连接")
        cursor = self._graph.run(cypher, params or {})
        return [dict(record) for record in cursor]

    def query(self, plan: QueryPlan) -> dict:
        """把一个 QueryPlan 翻译成 Cypher 并执行，返回统一结构的结果。"""
        cypher, params = build_cypher(plan)
        result: dict = {"rows": [], "direct": [], "path": None, "cypher": cypher, "params": params}

        if plan.kind == "compare":
            a, b = plan.entities[0]["name"], plan.entities[1]["name"]
            result["direct"] = self.run(
                """
MATCH (a:Entity {name: $a})-[r]-(b:Entity {name: $b})
RETURN type(r) AS relation, coalesce(r.description, '') AS description,
       coalesce(r.confidence, 0.90) AS confidence, coalesce(r.evidence, '') AS evidence
""",
                {"a": a, "b": b},
            )
            result["path"] = self._shortest_path(a, b)
            result["rows"] = self.run(cypher, params)
            return result

        rows = self.run(cypher, params)
        if plan.kind == "path":
            # 直接边优先：两个实体之间只要有边，就返回直接关系，不强行凑多跳链
            a, b = plan.entities[0]["name"], plan.entities[1]["name"]
            result["direct"] = self._direct_lookup(a, b)
            if result["direct"]:
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

        result["rows"] = rows
        return result

    def _direct_lookup(self, a: str, b: str) -> list[dict]:
        """查 a、b 之间的直接边（无向匹配），供 path 意图"直接边优先"规则使用。"""
        return self.run(
            """
MATCH (a:Entity {name: $a})-[r]-(b:Entity {name: $b})
RETURN type(r) AS relation, coalesce(r.description, '') AS description,
       coalesce(r.confidence, 0.90) AS confidence, coalesce(r.evidence, '') AS evidence
ORDER BY coalesce(r.confidence, 0.90) DESC
""",
            {"a": a, "b": b},
        )

    def _shortest_path(self, a: str, b: str) -> dict | None:
        """
        无向最短路径，但在多个等长候选里择优，避免"方向颠倒 + 低置信"的链路：
          1. 取全部最短路径候选（最多 5 条）；
          2. 过滤含「属于」反向跳的候选（属于边只能 子类→父类 遍历）；
          3. 剩余候选按最小置信度降序、跳数升序取第一。
        每跳同时返回 startNode/endNode 名字，解释层按真实方向渲染箭头。
        """
        rows = self.run(
            """
MATCH (a:Entity {name: $a}), (b:Entity {name: $b})
MATCH p = allShortestPaths((a)-[*..6]-(b))
WITH p LIMIT 5
RETURN length(p) AS hops,
       [n IN nodes(p) | n.name] AS path_names,
       [r IN relationships(p) | type(r)] AS path_rels,
       [r IN relationships(p) | coalesce(r.description, '')] AS path_descs,
       [r IN relationships(p) | coalesce(r.confidence, 0.90)] AS path_confs,
       [r IN relationships(p) | startNode(r).name] AS path_srcs,
       [r IN relationships(p) | endNode(r).name] AS path_tgts
""",
            {"a": a, "b": b},
        )
        candidates = [self._row_to_path(r) for r in rows]
        return _pick_path(candidates)

    @staticmethod
    def _row_to_path(row: dict) -> dict:
        return {
            "hops": row["hops"],
            "names": row["path_names"],
            "rels": row["path_rels"],
            "descs": row.get("path_descs", []),
            "confs": row.get("path_confs", []),
            "srcs": row.get("path_srcs", []),
            "tgts": row.get("path_tgts", []),
        }
    # -- 整图 ----------------------------------------------------------
    def graph_data(self) -> dict:
        nodes = self.run(
            """
MATCH (n:Entity)
OPTIONAL MATCH (n)-[r]-()
RETURN n.name AS id, coalesce(n.id, '') AS eid, n.type AS type, coalesce(n.en_name, '') AS en_name,
       coalesce(n.aliases, '') AS aliases, coalesce(n.description, '') AS description,
       n.color AS color, count(r) AS degree,
       coalesce(n.confidence, 1.0) AS confidence, coalesce(n.source, '') AS source
ORDER BY n.type, n.name
"""
        )
        links = self.run(
            """
MATCH (a:Entity)-[r]->(b:Entity)
RETURN a.name AS source, b.name AS target, type(r) AS type,
       coalesce(r.description, '') AS description, coalesce(r.symmetric, false) AS symmetric,
       coalesce(r.confidence, 0.90) AS confidence, coalesce(r.evidence, '') AS evidence
"""
        )
        # 数据库里可能没存 label/color（阶段2 只写了类型），这里按类型补齐，
        # 保证 Neo4j 模式与本地模式的响应字段完全一致，前端不用做兼容。
        for n in nodes:
            n["label"] = TYPE_TO_LABEL.get(n.get("type"), "Entity")
            n["color"] = n.get("color") or TYPE_COLORS.get(n.get("type"), "#999999")
            n["degree"] = int(n.get("degree") or 0)
            n["confidence"] = float(n.get("confidence") or 1.0)
        for l in links:
            l["confidence"] = float(l.get("confidence") or 0.90)
        return {"nodes": nodes, "links": links}


# ---------------------------------------------------------------------------
# 二、本地图后端（降级 / 对照）
# ---------------------------------------------------------------------------
class LocalStore:
    """在内存图上实现与 Cypher 完全等价的结果（字段名保持一致）。"""

    def query(self, plan: QueryPlan) -> dict:
        result: dict = {"rows": [], "direct": [], "path": None, "cypher": build_cypher(plan)[0],
                        "params": build_cypher(plan)[1]}
        kind = plan.kind
        limit = max(1, min(plan.limit, settings.MAX_LIMIT))

        if kind in ("neighbors", "detail"):
            name = plan.entities[0]["name"]
            rels = plan.relations or None
            rows = []
            for rel, peer, direction, desc in kb.adj[name]:
                if rels and rel not in rels:
                    continue
                if plan.direction and direction != plan.direction:
                    continue
                if peer == name:
                    continue
                meta = kb.edge(name, peer, rel) if direction == "out" else kb.edge(peer, name, rel)
                rows.append({
                    "relation": rel, "direction": direction, "name": peer,
                    "type": kb.get(peer)["type"] if kb.get(peer) else "", "description": desc,
                    "confidence": meta["confidence"],
                })
            rows.sort(key=lambda r: (r["relation"], r["name"]))
            result["rows"] = rows[:limit]

        elif kind == "filter":
            rels = plan.relations or ["属于", "衍生自", "基于", "可对比", "应用于"]
            rows = []
            for rel, peer, direction, desc in kb.adj[plan.target]:
                if direction != "in" or rel not in rels:
                    continue
                node = kb.get(peer)
                if not node or (plan.entity_type and node["type"] != plan.entity_type):
                    continue
                meta = kb.edge(peer, plan.target, rel)
                rows.append({"name": peer, "type": node["type"],
                             "description": node.get("description", ""),
                             "relation": rel, "rel_description": desc,
                             "confidence": meta["confidence"], "evidence": meta["evidence"],
                             "target": plan.target})
            rows.sort(key=lambda r: r["name"])
            result["rows"] = rows[:limit]

        elif kind == "type_list":
            rows = [{"name": e["name"], "type": e["type"], "description": e.get("description", "")}
                    for e in kb.type_index.get(plan.entity_type, [])]
            rows.sort(key=lambda r: r["name"])
            result["rows"] = rows[:limit]

        elif kind == "compare":
            a, b = plan.entities[0]["name"], plan.entities[1]["name"]
            result["direct"] = self._direct_edges(a, b)
            result["rows"] = self._common_neighbors(a, b)[:limit]
            result["path"] = self._bfs_path(a, b)

        elif kind == "path":
            a, b = plan.entities[0]["name"], plan.entities[1]["name"]
            # 直接边优先：与 Neo4jStore 行为保持一致
            direct = self._direct_edges(a, b)
            if direct:
                result["direct"] = direct
                result["path"] = None
                result["rows"] = []
                return result
            path = self._bfs_path(a, b)
            result["path"] = path
            result["rows"] = ([{"hops": path["hops"], "path_names": path["names"],
                                "path_rels": path["rels"], "path_descs": path["descs"],
                                "path_confs": path["confs"], "path_srcs": path.get("srcs", []),
                                "path_tgts": path.get("tgts", [])}] if path else [])

        elif kind == "stats":
            rows = []
            for e in kb.entities:
                if plan.entity_type and e["type"] != plan.entity_type:
                    continue
                rows.append({"name": e["name"], "type": e["type"], "degree": kb.degree.get(e["name"], 0)})
            rows.sort(key=lambda r: (-r["degree"], r["name"]))
            result["rows"] = rows[:limit]

        else:  # search
            rows = [{"name": e["name"], "type": e["type"], "description": e.get("description", "")}
                    for e in kb.search(plan.keyword or "")]
            rows.sort(key=lambda r: r["name"])
            result["rows"] = rows[:limit]

        return result

    # -- 本地图算法 ----------------------------------------------------
    @staticmethod
    def _direct_edges(a: str, b: str) -> list[dict]:
        seen, out = set(), []
        for rel, peer, direction, desc in kb.adj[a]:
            if peer == b and rel not in seen:
                seen.add(rel)
                meta = kb.edge(a, b, rel) if direction == "out" else kb.edge(b, a, rel)
                out.append({"relation": rel, "description": desc,
                            "confidence": meta["confidence"], "evidence": meta["evidence"]})
        return out

    @staticmethod
    def _common_neighbors(a: str, b: str) -> list[dict]:
        """共同邻居：a 和 b 各自的一跳邻居取交集（对应 Cypher 的双跳模式）。"""
        nb_a = {peer: rel for rel, peer, _d, _desc in kb.adj[a] if peer not in (a, b)}
        nb_b = {peer: rel for rel, peer, _d, _desc in kb.adj[b] if peer not in (a, b)}
        rows = []
        for name in sorted(set(nb_a) & set(nb_b)):
            node = kb.get(name) or {}
            rows.append({
                "name": name, "type": node.get("type", ""),
                "rel_a": nb_a[name], "rel_b": nb_b[name],
                "description": node.get("description", ""),
            })
        return rows

    @staticmethod
    def _bfs_path(a: str, b: str) -> dict | None:
        """无权图最短路径（BFS），与 Cypher shortestPath 结果一致；同时记录每跳真实方向。"""
        if a == b:
            return {"hops": 0, "names": [a], "rels": [], "descs": [], "confs": [],
                    "srcs": [], "tgts": []}
        prev: dict[str, tuple | None] = {a: None}
        queue = deque([a])
        while queue:
            cur = queue.popleft()
            if cur == b:
                break
            for rel, peer, direction, desc in kb.adj[cur]:
                if peer not in prev:
                    meta = kb.edge(cur, peer, rel)
                    # direction='out' 表示 cur→peer，否则是 peer→cur，记下真实方向
                    src, tgt = (cur, peer) if direction == "out" else (peer, cur)
                    prev[peer] = (cur, rel, desc, meta["confidence"], src, tgt)
                    queue.append(peer)
        if b not in prev:
            return None
        names, rels, descs, confs, srcs, tgts = [b], [], [], [], [], []
        cur = b
        while prev[cur] is not None:
            parent, rel, desc, conf, src, tgt = prev[cur]
            names.append(parent)
            rels.append(rel)
            descs.append(desc)
            confs.append(conf)
            srcs.append(src)
            tgts.append(tgt)
            cur = parent
        return {
            "hops": len(rels),
            "names": list(reversed(names)),
            "rels": list(reversed(rels)),
            "descs": list(reversed(descs)),
            "confs": list(reversed(confs)),
            "srcs": list(reversed(srcs)),
            "tgts": list(reversed(tgts)),
        }

    def graph_data(self) -> dict:
        return kb.to_graph()


# ---------------------------------------------------------------------------
# 三、后端选择
# ---------------------------------------------------------------------------
neo4j_store = Neo4jStore()
local_store = LocalStore()

from .pg import PostgresStore    # noqa: E402  （放在类定义之后，避免与 kgdata 的导入顺序纠缠）

postgres_store = PostgresStore()


def pick_store() -> tuple[object, str, bool]:
    """
    按 KG_MODE 选择后端。
    返回 (store, 实际使用的后端名, 是否降级)。

    三种真实后端各有分工：
        neo4j    —— 本机演示的图数据库（Cypher，多跳路径最自然）
        postgres —— 线上托管的 PostgreSQL（SQL，数据持久化在云上，换设备也在）
        local    —— 服务端内存图（data/kg.json 兜底，答辩现场数据库挂了也能讲）
    auto 的优先级：neo4j → postgres → local，谁配了、连得上就用谁。
    """
    mode = settings.KG_MODE
    if mode == "local":
        return local_store, "local", True
    if mode == "neo4j":
        return neo4j_store, "neo4j", False
    if mode in ("postgres", "postgresql", "pg"):
        return postgres_store, "postgres", False
    # auto：图库优先，其次是托管 PostgreSQL，最后才是本地内存图
    if neo4j_store.connect():
        return neo4j_store, "neo4j", False
    if postgres_store.connect():
        return postgres_store, "postgres", False
    return local_store, "local", True
