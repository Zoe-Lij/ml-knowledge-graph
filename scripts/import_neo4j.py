#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
import_neo4j.py
---------------
用 py2neo 把阶段1的 CSV 数据自动写入 Neo4j（课程设计阶段2 的核心脚本）。

数据来源：
    data/entities.csv        113 个实体（提供类型 → 决定节点标签）
    data/entity_relation.csv 258 条三元组（head, relation, tail）

写入策略（为什么这样设计）：
    1. 先建唯一约束，再 MERGE 节点 —— 保证脚本可以反复执行而不会产生重复数据；
    2. 节点按类型分组写入，关系按关系类型分组写入 —— 因为 Cypher 的
       标签/关系类型不能用参数传递，只能拼进语句，分组后每类只执行一条批量语句；
    3. 用 UNWIND $rows 批量提交（默认 200 条/批）—— 比一行一条快一个数量级；
    4. 所有拼接进 Cypher 的标签/关系类型都来自白名单校验 —— 防止注入。

用法：
    # 1) 先看导入计划，不连数据库（改完数据先跑这个）
    python scripts/import_neo4j.py --dry-run

    # 2) 正式导入（会自动清空旧数据）
    python scripts/import_neo4j.py --reset

    # 3) 指定连接信息
    python scripts/import_neo4j.py --uri bolt://localhost:7687 --user neo4j --password '<你的Neo4j密码>'

依赖：
    pip install py2neo==2021.2.4   （Neo4j 4.4.x 配套版本，见 docs/02_neo4j_setup.md）
"""

import argparse
import csv
import os
import sys
import time
from collections import Counter, defaultdict

# 项目根目录：本文件在 <root>/scripts/ 下
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR = os.path.join(ROOT, "data")

# 中文类型 → Neo4j 标签（必须与 01_schema.cypher 保持一致）
TYPE_TO_LABEL = {
    "机器学习概念": "Concept",
    "算法": "Algorithm",
    "模型": "Model",
    "评价指标": "Metric",
}

# 5 种关系类型（白名单：只允许这 5 个拼进 Cypher）
ALLOWED_RELATIONS = ["属于", "衍生自", "基于", "可对比", "应用于"]

# 关系描述模板：三元组表没有描述列，这里用模板补一句人话（前端悬浮提示用）
REL_TEMPLATES = {
    "属于": "{h}属于{t}",
    "衍生自": "{h}由{t}改进衍生而来",
    "基于": "{h}的实现依赖{t}",
    "可对比": "{h}与{t}常被放在一起横向对比",
    "应用于": "{h}可应用于{t}",
}

# 期望规模（用于导入后自检，不一致时给出警告）
EXPECTED_NODES = 113
EXPECTED_RELS = 258


# ---------------------------------------------------------------------------
# 一、配置读取：支持 .env 文件，也支持命令行/环境变量覆盖
# ---------------------------------------------------------------------------
def load_dotenv(path):
    """极简 .env 解析（避免为一个小功能引入额外依赖）。已存在的环境变量优先。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# ---------------------------------------------------------------------------
# 二、读取 CSV
# ---------------------------------------------------------------------------
def read_entities(path):
    """读实体表 → {name: {id, name, type, en_name, aliases, description, color}}"""
    if not os.path.exists(path):
        sys.exit("[ERROR] 找不到实体表：%s（请先运行 python tools/build_dataset.py）" % path)
    entities = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            if row["type"] not in TYPE_TO_LABEL:
                sys.exit("[ERROR] 实体 %s 的类型 '%s' 不在允许的 4 类内" % (name, row["type"]))
            entities[name] = {
                "id": row["id"].strip(),
                "name": name,
                "type": row["type"].strip(),
                "en_name": row.get("en_name", "").strip(),
                "aliases": row.get("aliases", "").strip(),
                "description": row.get("description", "").strip(),
                "color": row.get("color", "").strip(),
                # 置信度（阶段1 优化点）：缺列时给 1.0，保证老 CSV 也能导入
                "confidence": float(row.get("confidence") or 1.0),
                "source": (row.get("source") or "").strip(),
            }
    return entities


def read_triples(path, meta=None):
    """读三元组表 → [{head, relation, tail, description, confidence, evidence}]"""
    meta = meta or {}
    if not os.path.exists(path):
        sys.exit("[ERROR] 找不到三元组表：%s" % path)
    triples = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = {"head", "relation", "tail"} - set(reader.fieldnames or [])
        if missing:
            sys.exit("[ERROR] entity_relation.csv 缺少字段：%s" % missing)
        for lineno, row in enumerate(reader, start=2):
            head = (row["head"] or "").strip()
            rel = (row["relation"] or "").strip()
            tail = (row["tail"] or "").strip()
            if not head or not rel or not tail:
                sys.exit("[ERROR] 第 %d 行存在空值：%s" % (lineno, row))
            if rel not in ALLOWED_RELATIONS:
                sys.exit("[ERROR] 第 %d 行关系类型 '%s' 不在允许的 5 类内" % (lineno, rel))
            m = meta.get((head, rel, tail)) or meta.get((tail, rel, head)) or {}
            triples.append({
                "head": head,
                "relation": rel,
                "tail": tail,
                "description": m.get("description") or REL_TEMPLATES[rel].format(h=head, t=tail),
                "confidence": m.get("confidence", 0.90),
                "evidence": m.get("evidence", "未标注"),
                "symmetric": m.get("symmetric", rel == "可对比"),
            })
    return triples


def read_relation_meta(path):
    """
    从 relations.csv 读关系元信息（置信度 / 依据 / 描述 / 是否对称）。
    为什么要单独读一张表：entity_relation.csv 是课程要求的严格三列格式，
    不能加列；置信度这类"优化字段"放在 relations.csv 里，按三元组对齐即可。
    文件不存在时返回空字典，导入照常进行（边上的 confidence 会给默认值）。
    """
    meta = {}
    if not os.path.exists(path):
        print("[WARN] 未找到 relations.csv，关系置信度将使用默认值 0.90")
        return meta
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["head_name"].strip(), row["relation"].strip(), row["tail_name"].strip())
            meta[key] = {
                "confidence": float(row.get("confidence") or 0.90),
                "evidence": (row.get("evidence") or "未标注").strip(),
                "description": (row.get("description") or "").strip(),
                "symmetric": (row.get("symmetric") or "false").strip() == "true",
            }
    return meta


# ---------------------------------------------------------------------------
# 三、把数据按"标签 / 关系类型"分组（Cypher 不支持参数化标签，必须分组批量写）
# ---------------------------------------------------------------------------
def group_nodes(entities):
    grouped = defaultdict(list)
    for e in entities.values():
        grouped[TYPE_TO_LABEL[e["type"]]].append(e)
    return grouped


def group_relations(triples):
    grouped = defaultdict(list)
    for t in triples:
        grouped[t["relation"]].append(t)
    return grouped


def chunks(seq, size):
    """把列表切成固定大小的批次，配合 UNWIND 批量提交"""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
# 四、Cypher 模板
# ---------------------------------------------------------------------------
# 注意：Cypher 里的 {name: row.name} 是 map 字面量，在 .format() 中要写成双花括号
NODE_CYPHER = """
UNWIND $rows AS row
MERGE (n:Entity:`{label}` {{name: row.name}})
SET n.id          = row.id,
    n.type        = row.type,
    n.en_name     = row.en_name,
    n.aliases     = row.aliases,
    n.description = row.description,
    n.color       = row.color,
    n.confidence  = row.confidence,
    n.source      = row.source
RETURN count(n) AS c
"""

REL_CYPHER = """
UNWIND $rows AS row
MATCH (a:Entity {{name: row.head}})
MATCH (b:Entity {{name: row.tail}})
MERGE (a)-[r:`{rel}`]->(b)
SET r.description = row.description,
    r.confidence  = row.confidence,
    r.evidence    = row.evidence,
    r.symmetric   = row.symmetric{extra}
RETURN count(r) AS c
"""

CONSTRAINT_CYPHER = [
    "CREATE CONSTRAINT entity_name_unique IF NOT EXISTS "
    "FOR (n:Entity) REQUIRE n.name IS UNIQUE",
    "CREATE INDEX entity_type_index IF NOT EXISTS "
    "FOR (n:Entity) ON (n.type)",
]


# ---------------------------------------------------------------------------
# 五、导入主流程
# ---------------------------------------------------------------------------
def print_plan(entities, triples):
    """打印导入计划（--dry-run 用，不连数据库也能检查数据）"""
    type_counter = Counter(e["type"] for e in entities.values())
    rel_counter = Counter(t["relation"] for t in triples)

    print("=" * 62)
    print("导入计划（dry-run，未连接数据库）")
    print("=" * 62)
    conf = Counter("%.2f" % t["confidence"] for t in triples)
    low = sum(1 for t in triples if t["confidence"] < 0.80)
    print("实体：%d 个" % len(entities))
    for t, label in TYPE_TO_LABEL.items():
        print("  - %-8s %3d 个  →  标签 :%s" % (t, type_counter.get(t, 0), label))
    print("三元组：%d 条" % len(triples))
    for r in ALLOWED_RELATIONS:
        print("  - %-4s %3d 条" % (r, rel_counter.get(r, 0)))
    print("关系置信度：" + "，".join("%s=%d" % (k, v) for k, v in sorted(conf.items(), reverse=True))
          + "（< 0.80 待复核 %d 条）" % low)
    print("-" * 62)
    print("节点写入语句示例：")
    print(NODE_CYPHER.format(label="Algorithm").strip())
    print("-" * 62)
    print("关系写入语句示例：")
    print(REL_CYPHER.format(rel="属于", extra="").strip())
    print("=" * 62)


def check_consistency(entities, triples):
    """检查三元组里的实体名是否都能在实体表中找到（防止悬空边）"""
    missing = set()
    for t in triples:
        for side in ("head", "tail"):
            if t[side] not in entities:
                missing.add(t[side])
    if missing:
        sys.exit("[ERROR] 以下实体出现在三元组中但不在 entities.csv 里：%s" % "、".join(sorted(missing)))
    print("[CHECK] 三元组实体全部命中实体表，共 %d 个实体、%d 条关系" % (len(entities), len(triples)))


def connect(uri, user, password):
    """连接 Neo4j，并把常见错误翻译成"人话"提示"""
    try:
        from py2neo import Graph
    except ImportError:
        sys.exit("[ERROR] 未安装 py2neo，请执行：pip install py2neo==2021.2.4")

    print("[CONNECT] %s  (user=%s)" % (uri, user))
    try:
        graph = Graph(uri, auth=(user, password), name="neo4j")
        version = graph.run("CALL dbms.components() YIELD versions "
                            "RETURN versions[0] AS v").evaluate()
        print("[CONNECT] 成功，Neo4j 版本：%s" % version)
        return graph
    except Exception as exc:  # 连接类异常统一给出排查建议
        msg = str(exc)
        print("\n[ERROR] 连接失败：%s" % msg)
        if "401" in msg or "authentication" in msg.lower() or "Unauthorized" in msg:
            print("  → 账号密码不对：先在 Neo4j Browser（http://localhost:7474）确认密码，"
                  "首次登录必须改掉默认密码。")
        elif "No matching Bolt" in msg or "handshake" in msg.lower():
            print("  → Bolt 协议版本不匹配：py2neo 2021.2.4 只兼容 Neo4j 4.4，"
                  "若你装的是 Neo4j 5.x，请换成 4.4.x（见 docs/02_neo4j_setup.md）。")
        else:
            print("  → 常见原因：① 数据库没启动；② 端口写成 7474（HTTP）而不是 7687（Bolt）；"
                  "③ 用了 http:// 前缀（应为 bolt://）。")
        sys.exit(1)


def import_all(graph, entities, triples, batch_size):
    start = time.time()

    # ---- 1. 约束与索引（重复执行安全） ----
    for stmt in CONSTRAINT_CYPHER:
        graph.run(stmt)
    print("[SCHEMA] 唯一约束与 type 索引已就绪")

    # ---- 2. 节点：按标签分组批量 MERGE ----
    node_total = 0
    for label, rows in group_nodes(entities).items():
        cypher = NODE_CYPHER.format(label=label)
        for i, batch in enumerate(chunks(rows, batch_size), start=1):
            graph.run(cypher, rows=batch)
            node_total += len(batch)
        print("[NODE] :%-9s 写入 %3d 个" % (label, len(rows)))

    # ---- 3. 关系：按关系类型分组批量 MERGE ----
    rel_total = 0
    rel_grouped = group_relations(triples)
    for rel in ALLOWED_RELATIONS:
        rows = rel_grouped.get(rel, [])
        if not rows:
            continue
        extra = ", r.symmetric = true" if rel == "可对比" else ""
        cypher = REL_CYPHER.format(rel=rel, extra=extra)
        for batch in chunks(rows, batch_size):
            graph.run(cypher, rows=batch)
            rel_total += len(batch)
        print("[REL ] :%-5s  写入 %3d 条" % (rel, len(rows)))

    # ---- 4. 导入后核对（以数据库里的真实数量为准） ----
    db_nodes = graph.run("MATCH (n:Entity) RETURN count(n)").evaluate()
    db_rels = graph.run("MATCH ()-[r]->() RETURN count(r)").evaluate()
    cost = time.time() - start

    print("-" * 62)
    print("写入完成：节点 %d，关系 %d，耗时 %.2f 秒" % (node_total, rel_total, cost))
    print("数据库实际：节点 %d（期望 %d），关系 %d（期望 %d）"
          % (db_nodes, EXPECTED_NODES, db_rels, EXPECTED_RELS))
    if db_nodes != EXPECTED_NODES or db_rels != EXPECTED_RELS:
        print("[WARN] 数量与阶段1 的统计不一致，请检查 data/ 下 CSV 是否被改动")
    else:
        print("[PASS] 数量校验通过，数据入库完成 ✅")


def main():
    parser = argparse.ArgumentParser(description="把 data/*.csv 导入 Neo4j（py2neo）")
    parser.add_argument("--uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
                        help="Bolt 连接地址，默认 bolt://localhost:7687")
    parser.add_argument("--user", default=os.getenv("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.getenv("NEO4J_PASSWORD", ""))
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="CSV 所在目录")
    parser.add_argument("--batch", type=int, default=200, help="每批提交条数，默认 200")
    parser.add_argument("--reset", action="store_true", help="导入前先清空数据库")
    parser.add_argument("--dry-run", action="store_true", help="只打印导入计划，不连数据库")
    args = parser.parse_args()

    load_dotenv(os.path.join(ROOT, ".env"))

    entities = read_entities(os.path.join(args.data_dir, "entities.csv"))
    rel_meta = read_relation_meta(os.path.join(args.data_dir, "relations.csv"))
    triples = read_triples(os.path.join(args.data_dir, "entity_relation.csv"), rel_meta)

    # 三元组字段：写入数据库需要的字段（含置信度 / 依据 / 是否对称）
    triples = [{"head": t["head"], "relation": t["relation"], "tail": t["tail"],
                "description": t["description"], "confidence": t["confidence"],
                "evidence": t["evidence"], "symmetric": t["symmetric"]} for t in triples]

    print_plan(entities, triples)
    check_consistency(entities, triples)

    if args.dry_run:
        print("\n[dry-run] 未写入数据库。去掉 --dry-run 即可正式导入。")
        return

    graph = connect(args.uri, args.user, args.password)

    if args.reset:
        graph.run("MATCH (n) DETACH DELETE n")
        print("[RESET] 旧数据已清空")

    import_all(graph, entities, triples, args.batch)


if __name__ == "__main__":
    main()
