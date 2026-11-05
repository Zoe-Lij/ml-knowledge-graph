#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
kgdata.py
----------
本地数据层：把阶段1 的 data/kg.json 读进内存，供三处使用：

    1. 实体词典 —— 自然语言问句里的实体识别（nl2cypher.py 用）；
    2. 离线图引擎 —— 没有 Neo4j 时也能完整回答问句（LocalGraph）；
    3. 实体元信息 —— 类型、别名、描述、配色（前端详情面板用）。

为什么要有一个"离线图引擎"？
    课程要求的关系只有 5 种、图只有 113 个节点，完全可以在内存里算。
    它的作用不是替代 Neo4j，而是：
      - 开发阶段（数据库没装/没启动）能立刻调试 NL→Cypher 逻辑；
      - 答辩现场数据库万一挂了，系统仍能演示（KG_MODE=auto 自动降级）；
      - 用它来交叉验证 Cypher 查询结果对不对（同一份数据两条实现）。

数据规模：113 个实体 / 257 条关系，读一次约 3ms，直接常驻内存即可。
"""

import json
from collections import defaultdict
from pathlib import Path

from .config import DATA_DIR, settings

# 关系类型白名单（与 Schema 严格一致，任何地方出现第 6 种都是 bug）
RELATION_TYPES = ["属于", "衍生自", "基于", "可对比", "应用于"]

# 中文实体类型 → Neo4j 标签
TYPE_TO_LABEL = {
    "机器学习概念": "Concept",
    "算法": "Algorithm",
    "模型": "Model",
    "评价指标": "Metric",
}

# 实体类型 → 前端配色（与 docs/01_schema_design.md 第 7 节一致）
TYPE_COLORS = {
    "机器学习概念": "#3B6FD4",
    "算法": "#7C5CD6",
    "模型": "#E08A2E",
    "评价指标": "#2E9E6B",
}


class KnowledgeBase:
    """知识图谱数据集（内存单例）。"""

    def __init__(self, path: Path | None = None, raw: dict | None = None, source: str = "kg.json"):
        """
        两种构造方式：
          KnowledgeBase()                    —— 读 data/kg.json（本地开发 / 数据库不通时的兜底）
          KnowledgeBase(raw=fetch_dataset()) —— 直接用数据库读回来的数据（线上主路径）
        """
        if raw is None:
            path = path or (DATA_DIR / "kg.json")
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)

        # 这份内存副本是从哪儿来的：postgres = 运行期数据全在数据库里；kg.json = 兜底种子
        self.source: str = source
        self.meta: dict = raw.get("meta", {})

        # 实体按 id 建索引，同时保留列表顺序（导出时按类型分组过，顺序稳定）
        self.entities: list[dict] = raw["entities"]
        self.by_id: dict[str, dict] = {e["id"]: e for e in self.entities}
        self.by_name: dict[str, dict] = {e["name"]: e for e in self.entities}

        # 关系：统一成 {source, target, type, description, symmetric}
        self.relations: list[dict] = raw["relations"]

        # 邻接表：name -> [(关系类型, 邻居name, 方向 out/in, 描述)]
        # 双向各存一条，查询"一跳邻居"时直接查表，O(度数)
        self.adj: dict[str, list[tuple]] = defaultdict(list)
        # 度数：前端节点半径 / 统计问句用
        self.degree: dict[str, int] = defaultdict(int)
        for r in self.relations:
            s = self.by_id[r["source"]]["name"]
            t = self.by_id[r["target"]]["name"]
            desc = r.get("description", "")
            self.adj[s].append((r["type"], t, "out", desc))
            self.adj[t].append((r["type"], s, "in", desc))
            self.degree[s] += 1
            self.degree[t] += 1

        # 边元信息：(源, 尾, 关系) -> {confidence, evidence}
        # 为什么不塞进 adj 的元组里：adj 在 store/explain 里被大量解包成 4 元组，
        # 多塞一个字段会牵动所有解包处，容易漏改；单独建索引最省事，查询也是 O(1)。
        self.edge_meta: dict[tuple, dict] = {}
        for r in self.relations:
            s_name = self.by_id[r["source"]]["name"]
            t_name = self.by_id[r["target"]]["name"]
            meta = {
                "confidence": float(r.get("confidence", 0.90)),
                "evidence": r.get("evidence", ""),
            }
            self.edge_meta[(s_name, t_name, r["type"])] = meta
            if r.get("symmetric"):
                self.edge_meta[(t_name, s_name, r["type"])] = meta

        # 关键词倒排：给"没识别出实体"时的模糊检索兜底
        self.type_index: dict[str, list[dict]] = defaultdict(list)
        for e in self.entities:
            self.type_index[e["type"]].append(e)

    # ------------------------------------------------------------------
    # 基础查询
    # ------------------------------------------------------------------
    def get(self, name: str) -> dict | None:
        return self.by_name.get(name)

    def edge(self, source: str, target: str, rel: str) -> dict:
        """取一条边的置信度元信息；查不到时给默认值（0.90 = 未标注）。"""
        return self.edge_meta.get((source, target, rel)) or {"confidence": 0.90, "evidence": ""}

    def names_of_type(self, etype: str) -> list[str]:
        return [e["name"] for e in self.type_index.get(etype, [])]

    def counts(self) -> dict:
        """按类型/关系类型统计，/api/health 与前端图例用。"""
        by_type = defaultdict(int)
        for e in self.entities:
            by_type[e["type"]] += 1
        by_rel = defaultdict(int)
        for r in self.relations:
            by_rel[r["type"]] += 1
        return {
            "entities": len(self.entities),
            "relations": len(self.relations),
            "by_type": dict(by_type),
            "by_relation": dict(by_rel),
        }

    def to_graph(self) -> dict:
        """整图导出（D3 力导向图直接可用）。"""
        nodes = [
            {
                "id": e["name"],                 # D3 用 name 做唯一键，读起来直观
                "eid": e["id"],                  # 原始编号（C01/A06/M18/P04）
                "type": e["type"],
                "label": e.get("label", TYPE_TO_LABEL.get(e["type"], "Entity")),
                "en_name": e.get("en_name", ""),
                "aliases": e.get("aliases", ""),
                "description": e.get("description", ""),
                "color": e.get("color", TYPE_COLORS.get(e["type"], "#999999")),
                "degree": self.degree.get(e["name"], 0),
                "confidence": float(e.get("confidence", 1.0)),
                "source": e.get("source", ""),
            }
            for e in self.entities
        ]
        links = [
            {
                "source": self.by_id[r["source"]]["name"],
                "target": self.by_id[r["target"]]["name"],
                "type": r["type"],
                "description": r.get("description", ""),
                "symmetric": bool(r.get("symmetric", False)),
                # 置信度：前端按 < 0.80 画成虚线/半透明，让"推断出来的边"一眼可辨
                "confidence": float(r.get("confidence", 0.90)),
                "evidence": r.get("evidence", ""),
            }
            for r in self.relations
        ]
        return {"nodes": nodes, "links": links, "meta": self.counts()}

    def search(self, keyword: str, limit: int = 50) -> list[dict]:
        """名称 / 别名 / 英文名 包含匹配，给兜底检索用。"""
        kw = keyword.strip().lower()
        if not kw:
            return []
        hits = []
        for e in self.entities:
            haystack = " ".join(
                [e["name"], e.get("en_name", ""), e.get("aliases", "")]
            ).lower()
            if kw in haystack:
                hits.append(e)
        return hits[:limit]


def _bootstrap() -> KnowledgeBase:
    """
    启动时决定这份词典从哪儿来（顺序很重要）：

      1. 配了 DATABASE_URL 且能连通 → 从 PostgreSQL 读整份数据集（线上主路径）。
         这样运行期的数据只有数据库一个来源，kg.json 不参与查询；
      2. 否则 → 读 data/kg.json（本地开发 / 数据库暂时不通时的兜底）。

    注意：这一步在 import 时执行一次。Serverless 冷启动多花 100~300ms 连接数据库，
    换来的是"词典与数据库永远一致"——改了库里的实体，重新提问就能识别，不用重新部署。
    """
    if settings.database_configured:
        try:
            from .pg import fetch_dataset

            raw = fetch_dataset()
            if raw.get("entities"):
                return KnowledgeBase(raw=raw, source="postgres")
        except Exception as exc:
            print(f"[kgdata] PostgreSQL 加载失败，回退 data/kg.json：{type(exc).__name__}: {exc}")
    return KnowledgeBase(source="kg.json")


# 模块级单例：整个进程只加载一次
kb = _bootstrap()
