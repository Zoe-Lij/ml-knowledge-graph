#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
import_aura.py —— 把 113 实体 / 258 关系灌入云端 Neo4j（Aura / 任意 Neo4j 5.x）

为什么不复用 import_neo4j.py？
    import_neo4j.py 用 py2neo 2021.2.4，只兼容本机 Neo4j 4.4 的旧版 Bolt 协议；
    Aura 托管实例是 Neo4j 5.x，py2neo 会在握手阶段报 "No matching Bolt version"。
    本脚本改用官方 neo4j 驱动（Bolt 5.x），写入语句、属性名、关系类型与本地完全一致，
    保证"线上查到的图"和"本机演示的图"是同一张图。

用法：
    # 0) 先看导入计划（不连库）
    python scripts/import_aura.py --dry-run

    # 1) 正式导入（推荐显式传，也可用环境变量 NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD）
    python scripts/import_aura.py \
        --uri neo4j+s://xxxxxxxx.databases.neo4j.io \
        --user neo4j --password 'Aura给的密码' --reset

    # 2) 只校验线上库里的数量对不对（不写入）
    python scripts/import_aura.py --verify

依赖：
    pip install "neo4j>=5.14,<6"
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 复用本地导入脚本的读取与分组逻辑（同一份 CSV 口径，避免两套解析）
from import_neo4j import (  # noqa: E402
    ALLOWED_RELATIONS, NODE_CYPHER, REL_CYPHER, TYPE_TO_LABEL,
    chunks, group_nodes, group_relations, read_entities, read_relation_meta,
    read_triples,
)

DATA_DIR = os.path.join(ROOT, "data")
BATCH = 200

CONSTRAINTS = [
    "CREATE CONSTRAINT entity_name_unique IF NOT EXISTS "
    "FOR (n:Entity) REQUIRE n.name IS UNIQUE",
    "CREATE INDEX entity_type_index IF NOT EXISTS FOR (n:Entity) ON (n.type)",
]


def load_data():
    entities = read_entities(os.path.join(DATA_DIR, "entities.csv"))
    meta = read_relation_meta(os.path.join(DATA_DIR, "relations.csv"))
    triples = read_triples(os.path.join(DATA_DIR, "entity_relation.csv"), meta)
    return entities, triples


def print_plan(entities, triples):
    from collections import Counter
    tc = Counter(e["type"] for e in entities.values())
    rc = Counter(t["relation"] for t in triples)
    print("=" * 64)
    print("云端导入计划（dry-run，未连接数据库）")
    print("=" * 64)
    print("实体 %d 个：%s" % (len(entities), "，".join("%s %d" % (k, v) for k, v in tc.items())))
    print("关系 %d 条：%s" % (len(triples), "，".join("%s %d" % (r, rc.get(r, 0))
                                                 for r in ALLOWED_RELATIONS)))
    print("节点标签：%s" % "，".join("%s→:%s" % (k, v) for k, v in TYPE_TO_LABEL.items()))
    print("=" * 64)


def connect(uri, user, password, database):
    try:
        from neo4j import GraphDatabase
    except ImportError:
        sys.exit('[ERROR] 未安装官方驱动，请执行：pip install "neo4j>=5.14,<6"')

    print("[CONNECT] %s (user=%s, db=%s)" % (uri, user, database))
    driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=15)
    try:
        recs, _s, _k = driver.execute_query(
            "CALL dbms.components() YIELD versions, edition "
            "RETURN versions[0] AS v, edition", database_=database)
        r = recs[0].data() if recs else {}
        print("[CONNECT] 成功：Neo4j %s (%s)" % (r.get("v", "?"), r.get("edition", "?")))
    except Exception as exc:
        driver.close()
        msg = str(exc)
        print("\n[ERROR] 连接失败：%s" % msg)
        low = msg.lower()
        if "unauthorized" in low or "authentication" in low or "401" in msg:
            print("  → 账号或密码不对。Aura 的初始密码只在创建实例时显示一次，"
                  "丢了就在控制台 Reset password。")
        elif "timeout" in low or "timed out" in low:
            print("  → 网络不通：确认 URI 是 neo4j+s:// 开头（不是 https），"
                  "本机能否访问 7687/7688 出站；公司网/VPN 可能拦截。")
        elif "certificate" in low or "ssl" in low:
            print("  → TLS 证书问题：Aura 必须用 neo4j+s://（+s 表示强制加密）。")
        sys.exit(1)
    return driver


def ensure_constraints(driver, database):
    for stmt in CONSTRAINTS:
        driver.execute_query(stmt, database_=database)
    print("[SCHEMA] 唯一约束与类型索引就绪")


def clear_all(driver, database):
    driver.execute_query("MATCH (n) DETACH DELETE n", database_=database)
    print("[RESET] 已清空库内原有节点与关系")


def insert(driver, entities, triples, database):
    # 1) 节点：按标签分组（标签不能参数化，只能拼进语句，标签来自白名单映射）
    total_n = 0
    for label, rows in group_nodes(entities).items():
        if label not in TYPE_TO_LABEL.values():
            sys.exit("[ERROR] 非法标签：%s" % label)
        stmt = NODE_CYPHER.format(label=label)
        for batch in chunks(rows, BATCH):
            driver.execute_query(stmt, parameters_={"rows": batch}, database_=database)
            total_n += len(batch)
        print("  [NODE] :%-10s %3d 个" % (label, len(rows)))

    # 2) 关系：按关系类型分组（同理，只在白名单内的 5 类才允许拼接）
    total_r = 0
    for rel, rows in group_relations(triples).items():
        if rel not in ALLOWED_RELATIONS:
            sys.exit("[ERROR] 非法关系类型：%s" % rel)
        stmt = REL_CYPHER.format(rel=rel, extra="")
        for batch in chunks(rows, BATCH):
            driver.execute_query(stmt, parameters_={"rows": batch}, database_=database)
            total_r += len(batch)
        print("  [REL ] %-4s %3d 条" % (rel, len(rows)))

    print("[IMPORT] 写入完成：%d 个节点 / %d 条关系" % (total_n, total_r))


def verify(driver, database, expect_n, expect_r):
    recs, _s, _k = driver.execute_query(
        "MATCH (n:Entity) RETURN count(n) AS c", database_=database)
    n = recs[0]["c"]
    recs, _s, _k = driver.execute_query(
        "MATCH (:Entity)-[r]->(:Entity) RETURN count(r) AS c", database_=database)
    r = recs[0]["c"]
    recs, _s, _k = driver.execute_query(
        "MATCH (:Entity)-[r]->() RETURN type(r) AS t, count(r) AS c ORDER BY c DESC",
        database_=database)
    dist = {x["t"]: x["c"] for x in recs}
    ok = (n == expect_n and r == expect_r)
    print("[VERIFY] 线上库：%d 节点 / %d 关系（期望 %d / %d）→ %s"
          % (n, r, expect_n, expect_r, "PASS" if ok else "MISMATCH"))
    print("[VERIFY] 关系分布：%s" % "，".join("%s %d" % (k, v) for k, v in dist.items()))
    return ok


def load_local_env() -> None:
    """从项目根 .env.vercel 读取云端凭据（存在才读，不覆盖已有环境变量）。"""
    p = os.path.join(ROOT, ".env.vercel")
    if not os.path.exists(p):
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v
    print("[ENV] 已读取 .env.vercel")


def main():
    load_local_env()
    ap = argparse.ArgumentParser(description="把知识图谱导入云端 Neo4j（Aura / Neo4j 5.x）")
    ap.add_argument("--uri", default=os.getenv("NEO4J_URI", ""))
    ap.add_argument("--user", default=os.getenv("NEO4J_USER", "neo4j"))
    ap.add_argument("--password", default=os.getenv("NEO4J_PASSWORD", ""))
    ap.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    ap.add_argument("--reset", action="store_true", help="导入前清空云端库（幂等重灌）")
    ap.add_argument("--dry-run", action="store_true", help="只打印导入计划，不连库")
    ap.add_argument("--verify", action="store_true", help="只校验数量，不写入")
    args = ap.parse_args()

    entities, triples = load_data()
    print_plan(entities, triples)
    if args.dry_run:
        return

    if not args.uri:
        sys.exit("[ERROR] 缺少 --uri（或环境变量 NEO4J_URI），形如 neo4j+s://xxxx.databases.neo4j.io")
    if not args.password:
        sys.exit("[ERROR] 缺少 --password（或环境变量 NEO4J_PASSWORD）")

    driver = connect(args.uri, args.user, args.password, args.database)
    try:
        if args.verify:
            ok = verify(driver, args.database, len(entities), len(triples))
            sys.exit(0 if ok else 2)
        ensure_constraints(driver, args.database)
        if args.reset:
            clear_all(driver, args.database)
        insert(driver, entities, triples, args.database)
        verify(driver, args.database, len(entities), len(triples))
    finally:
        driver.close()

    print("=" * 64)
    print("云端图库已就绪。接下来在 Vercel 配置环境变量：")
    print("  NEO4J_URI=%s" % args.uri)
    print("  NEO4J_USER=%s" % args.user)
    print("  NEO4J_PASSWORD=<保持机密>")
    print("  NEO4J_DATABASE=%s" % args.database)
    print("  KG_MODE=auto")
    print("=" * 64)


if __name__ == "__main__":
    main()
