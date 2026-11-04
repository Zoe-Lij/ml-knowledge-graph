#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
smoke_test_api.py
------------------
后端接口冒烟测试：启动服务后跑一遍，确认接口和问答链路都正常。

用法：
    # 1) 先启动后端（另开一个终端）
    uvicorn app.main:app --host 127.0.0.1 --port 8000

    # 2) 跑自测
    python scripts/smoke_test_api.py
    python scripts/smoke_test_api.py --base-url http://127.0.0.1:8010

只用标准库（urllib），不需要额外装 requests，答辩现场无需联网。
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

# 每条用例：(问句, 期望意图, 期望至少返回几条结果)
CASES = [
    ("什么是过拟合？", "detail", 1),
    ("随机森林属于哪一类算法？", "neighbors", 1),
    ("XGBoost 是从哪个算法改进来的？", "neighbors", 1),
    ("支持向量机的核心原理是什么？", "neighbors", 1),
    ("随机森林和梯度提升树有什么区别？", "compare", 1),
    ("随机森林和K均值聚类怎么连起来？", "path", 1),
    ("哪些算法可以做异常检测？", "filter", 1),
    ("有哪些评价指标？", "type_list", 10),
    ("哪个实体关系最多？", "stats", 1),
    ("MSE 用于什么？", "neighbors", 1),
    ("过拟合", "detail", 1),
]


def get_json(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def post_json(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="知识图谱后端接口自测")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="后端地址")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    passed, failed = 0, 0

    def check(name: str, ok: bool, detail: str = ""):
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  [PASS] {name}  {detail}")
        else:
            failed += 1
            print(f"  [FAIL] {name}  {detail}")

    # ---------- 1. 健康检查 ----------
    print("\n[1] 健康检查 /api/health")
    try:
        code, health = get_json(f"{base}/api/health")
    except urllib.error.URLError as exc:
        print(f"  [FAIL] 服务未启动或地址不对：{exc}")
        print(f"  提示：先运行 uvicorn app.main:app --host 127.0.0.1 --port 8000")
        return 1
    check("HTTP 200", code == 200)
    check("数据集规模 113 实体", health["local_dataset"]["entities"] == 113,
          f"entities={health['local_dataset']['entities']}")
    check("数据集规模 258 关系", health["local_dataset"]["relations"] == 258,
          f"relations={health['local_dataset']['relations']}")
    print(f"  数据源：{health['kg_mode']}；Neo4j 连接：{health['neo4j']['connected']}"
          + ("" if health["neo4j"]["connected"] else "（将使用本地图降级）"))

    # ---------- 2. 图谱接口 ----------
    print("\n[2] 图谱接口 /api/graph")
    code, graph = get_json(f"{base}/api/graph")
    nodes, links = graph["nodes"], graph["links"]
    check("HTTP 200", code == 200)
    check("节点数 = 113", len(nodes) == 113, f"nodes={len(nodes)}")
    check("关系数 = 258", len(links) == 258, f"links={len(links)}")
    ids = {n["id"] for n in nodes}
    dangling = [l for l in links if l["source"] not in ids or l["target"] not in ids]
    check("无悬空边", not dangling, f"dangling={len(dangling)}")
    check("节点含度数字段", all("degree" in n for n in nodes))

    # ---------- 3. 问答接口 ----------
    print("\n[3] 问答接口 /api/query（POST）")
    for question, expect_kind, min_rows in CASES:
        try:
            code, data = post_json(f"{base}/api/query", {"question": question})
            ok = (code == 200 and data["intent"] == expect_kind
                  and len(data["rows"]) >= min_rows and bool(data["answer"]))
            detail = f"intent={data['intent']} rows={len(data['rows'])}"
            if not ok:
                detail += f"（期望 {expect_kind}/{min_rows}+）"
            check(question, ok, detail)
        except Exception as exc:                      # 单条失败不影响其他用例
            check(question, False, f"{type(exc).__name__}: {exc}")

    # ---------- 4. GET 版本与异常参数 ----------
    print("\n[4] 兼容性检查")
    q = urllib.parse.urlencode({"q": "逻辑回归和SVM哪个好"})
    code, data = get_json(f"{base}/api/query?{q}")
    check("GET /api/query 可用", code == 200 and data["intent"] == "compare", f"intent={data['intent']}")

    for name, payload, expect in [("空问句返回 4xx", {"question": ""}, (400, 422)),
                                 ("limit 越界返回 4xx", {"question": "过拟合", "limit": 999}, (400, 422))]:
        try:
            post_json(f"{base}/api/query", payload)
            check(name, False, "未拦截")
        except urllib.error.HTTPError as exc:
            check(name, exc.code in expect, f"HTTP {exc.code}")

    code, ex = get_json(f"{base}/api/examples")
    check("/api/examples 可用", code == 200 and len(ex["examples"]) >= 5)

    # ---------- 汇总 ----------
    print("\n" + "=" * 60)
    print(f"测试完成：{passed} 项通过，{failed} 项失败")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
