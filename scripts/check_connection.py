#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_connection.py
-------------------
环境自检脚本：确认"Python 能不能连上 Neo4j""图里到底有没有数据"。

用途：
    - 阶段2 导入前后各跑一次；
    - 阶段5 联调时排查"接口查不到数据"到底是后端问题还是数据库问题；
    - 答辩现场开场先跑一次，确认演示环境正常。

用法：
    python scripts/check_connection.py
    python scripts/check_connection.py --uri bolt://localhost:7687 --password '<你的Neo4j密码>'
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from import_neo4j import connect, load_dotenv, ROOT  # noqa: E402  复用连接逻辑


def main():
    parser = argparse.ArgumentParser(description="检查 Neo4j 连接与数据状态")
    parser.add_argument("--uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--user", default=os.getenv("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.getenv("NEO4J_PASSWORD", ""))
    args = parser.parse_args()

    load_dotenv(os.path.join(ROOT, ".env"))
    graph = connect(args.uri, args.user, args.password)

    print("-" * 62)
    # 1. 数据库里的数据量
    nodes = graph.run("MATCH (n:Entity) RETURN count(n)").evaluate()
    rels = graph.run("MATCH ()-[r]->() RETURN count(r)").evaluate()
    print("节点数：%d（阶段1 期望 113）" % nodes)
    print("关系数：%d（阶段1 期望 257）" % rels)

    if nodes == 0:
        print("\n[提示] 库里没有数据，请执行：python scripts/import_neo4j.py --reset")
        return

    # 2. 类型分布
    print("\n实体类型分布：")
    for record in graph.run(
        "MATCH (n:Entity) RETURN n.type AS t, count(*) AS c ORDER BY c DESC"
    ):
        print("  %-8s %3d" % (record["t"], record["c"]))

    # 3. 关系类型分布
    print("\n关系类型分布：")
    for record in graph.run(
        "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY c DESC"
    ):
        print("  %-6s %3d" % (record["t"], record["c"]))

    # 4. 抽样一条路径，肉眼确认中文字段没乱码
    print("\n抽样验证（随机森林 → 集成学习）：")
    for record in graph.run(
        "MATCH (a:Entity {name:'随机森林'})-[r]->(b:Entity) "
        "RETURN a.name AS a, type(r) AS r, b.name AS b LIMIT 5"
    ):
        print("  %s -[%s]-> %s" % (record["a"], record["r"], record["b"]))

    print("-" * 62)
    print("[OK] 环境自检完成")


if __name__ == "__main__":
    main()
