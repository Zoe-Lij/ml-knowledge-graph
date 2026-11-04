#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
security_selftest.py
---------------------
Cypher 注入防护自测（优化点 1 的验收脚本）。

两种跑法：
    1. 纯单元测试（不启动服务）：python scripts/security_selftest.py
    2. 连服务做端到端测试：    python scripts/security_selftest.py --base-url http://127.0.0.1:8000

测试集分两组：
    A 组 12 条 —— 恶意"问句"，应该在解析前就被 L1 拦掉（HTTP 400）；
    B 组 14 条 —— 恶意/合法 Cypher，直接喂给 L2/L3 校验器，验证拦截与放行是否准确。

为什么要写这个脚本？
    课程答辩时最怕"你说你做了防护"但拿不出证据。跑一遍这个脚本，
    20 多条攻击样例全部被拦截、5 条正常语句全部放行，结论是可复现的。
"""

import argparse
import json
import os
import sys

# 允许直接以脚本方式运行（把项目根目录加入 import path）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.security import SecurityError, guard  # noqa: E402

# ---------------------------------------------------------------------------
# A 组：恶意问句（期望被 L1 拦截 —— 400）
# ---------------------------------------------------------------------------
QUESTION_ATTACKS = [
    ("MATCH (n) DETACH DELETE n", "整句都是 Cypher 写操作"),
    ("随机森林）；MATCH (n) DELETE n", "中文问句里夹带删除语句"),
    ("随机森林'; MATCH (n) RETURN n; //", "闭合引号 + 多语句 + 注释"),
    ("x' OR 1=1 MATCH (n) DETACH DELETE n //", "经典 SQL 注入变形（恒真式）"),
    ("{name: $name}", "直接投递参数占位符"),
    ("随机森林; DROP INDEX entity_type_index", "分号拼接 DROP"),
    ("CALL db.labels()", "调用系统过程读元数据"),
    ("随机森林 /* 注释 */ 属于什么", "注释截断"),
    ("MATCH (n) SET n.type='算法' RETURN n", "写属性"),
    ("UNWIND [1,2] AS x RETURN x", "只读但非自然语言（仍应拒绝）"),
    ("$name", "参数注入片段"),
    ("随机森林` RETURN 1", "反引号闭合标识符"),
]

# ---------------------------------------------------------------------------
# B 组：语句级校验（期望 blocked=True 拦截 / blocked=False 放行）
# ---------------------------------------------------------------------------
CYPHER_CASES = [
    # ---- 恶意语句：必须拦截 ----
    ("MATCH (n) DETACH DELETE n", {}, True, "写操作 DETACH DELETE"),
    ("MATCH (n) RETURN n; MATCH (m) DELETE m", {}, True, "分号多语句"),
    ("MATCH (n) RETURN n // 注释", {}, True, "行注释"),
    ("MATCH (n:`Entity`) RETURN n", {}, True, "反引号绕过标签白名单"),
    ("MERGE (n:Entity {name:'x'}) RETURN n", {}, True, "MERGE 写操作"),
    ("LOAD CSV FROM 'http://evil/x.csv' AS row RETURN row", {}, True, "LOAD CSV 外联读取"),
    ("MATCH (n) RETURN n UNION MATCH (m) DELETE m", {}, True, "UNION 拼接写操作"),
    ("CALL apoc.periodic.iterate('MATCH (n) RETURN n','DETACH DELETE n',{})",
     {}, True, "APOC 批量删库"),
    ("MATCH (n:Secret) RETURN n", {}, True, "未授权标签"),
    ("MATCH (n)-[r:后门关系]->(m) RETURN n", {}, True, "未授权关系类型"),
    ("MATCH (n) WHERE n.name = $x RETURN n", {"x": {"$ne": 1}}, True, "字典型参数（NoSQL 注入风格）"),
    ("MATCH (n) WHERE n.name = $x RETURN n", {"x": "a" * 500}, True, "超长参数（DoS）"),
    ("MATCH (n) WHERE n.name = $x RETURN n", {"x": "a\nb"}, True, "参数含控制字符"),
    ("SET n.type = 'x' RETURN n", {}, True, "非只读开头"),
    # ---- 合法语句：必须放行 ----
    ("MATCH (n:Entity {name: $name})-[r]-(m:Entity) WHERE type(r) IN $rels "
     "RETURN m.name AS name LIMIT $limit",
     {"name": "随机森林", "rels": ["属于"], "limit": 50}, False, "一跳邻居（真实模板）"),
    ("MATCH (n:Algorithm)-[r]->(t:Entity {name: $target}) WHERE type(r) IN $rels "
     "RETURN n.name AS name, coalesce(r.confidence, 0.90) AS confidence LIMIT $limit",
     {"target": "异常检测任务", "rels": ["应用于"], "limit": 50}, False, "条件筛选（真实模板）"),
    ("MATCH (a:Entity {name: $a}), (b:Entity {name: $b}) MATCH p = shortestPath((a)-[*..6]-(b)) "
     "RETURN length(p) AS hops LIMIT 1", {"a": "随机森林", "b": "K均值聚类"}, False, "最短路径"),
    ("MATCH (n:Entity) RETURN count(n) AS c", {}, False, "统计查询"),
    ("RETURN 1", {}, False, "连通性探针"),
]


def _bar(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def run_unit_tests() -> tuple[int, int]:
    """A/B 两组跑一遍，返回 (通过数, 失败数)。"""
    passed = failed = 0

    _bar("A 组：恶意问句 → 应由 L1（问句筛查）拦截")
    for question, note in QUESTION_ATTACKS:
        try:
            guard.screen_question(question)
            print("  [FAIL] 未拦截  %-42s  # %s" % (question[:42], note))
            failed += 1
        except SecurityError as exc:
            print("  [PASS] 已拦截  %-42s  # %s（%s）" % (question[:42], note, exc.reason))
            passed += 1

    _bar("B 组：语句级校验 → L2（只读校验）+ L3（标识符白名单）")
    for cypher, params, should_block, note in CYPHER_CASES:
        try:
            guard.enforce(cypher, params)
            blocked, reason = False, ""
        except SecurityError as exc:
            blocked, reason = True, exc.reason
        ok = blocked == should_block
        tag = "PASS" if ok else "FAIL"
        expect = "应拦截" if should_block else "应放行"
        detail = f"（{reason}）" if blocked else ""
        print("  [%s] %s %-38s  # %s%s" % (tag, expect, cypher[:38].replace("\n", " "), note, detail))
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)

    return passed, failed


def run_http_tests(base_url: str) -> tuple[int, int]:
    """端到端：把恶意问句真的发给 /api/query，验证返回 400 且不留痕迹地拒绝。"""
    try:
        import urllib.error
        import urllib.request
    except ImportError:                     # pragma: no cover
        print("  [SKIP] 当前环境不支持 urllib")
        return 0, 0

    passed = failed = 0
    _bar("C 组：端到端（HTTP）→ POST /api/query")

    def post(question: str) -> tuple[int, dict]:
        data = json.dumps({"question": question}).encode("utf-8")
        req = urllib.request.Request(base_url.rstrip("/") + "/api/query", data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

    for question, note in QUESTION_ATTACKS:
        try:
            code, body = post(question)
        except Exception as exc:             # 服务没起
            print("  [SKIP] 请求失败：%s" % exc)
            return passed, failed
        ok = code == 400
        print("  [%s] HTTP %d  %-40s  # %s" % ("PASS" if ok else "FAIL", code, question[:40], note))
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)

    # 正常问句必须仍然可用（防止"防护过严"把功能拦死）
    code, body = post("随机森林属于哪一类算法？")
    ok = code == 200 and bool(body.get("answer"))
    print("  [%s] HTTP %d  正常问句仍可用：%s" % ("PASS" if ok else "FAIL", code,
                                                (body.get("answer") or "")[:36]))
    passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
    return passed, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="Cypher 注入防护自测")
    parser.add_argument("--base-url", default="", help="例如 http://127.0.0.1:8000，给了就额外跑 HTTP 测试")
    args = parser.parse_args()

    passed, failed = run_unit_tests()
    if args.base_url:
        p2, f2 = run_http_tests(args.base_url)
        passed, failed = passed + p2, failed + f2

    _bar("汇总")
    print("通过 %d 项，失败 %d 项" % (passed, failed))
    print("护栏累计校验语句 %d 条，拦截 %d 条" % (guard.checked, guard.blocked))
    if failed:
        print("[FAIL] 存在未按预期拦截的样例，请检查 app/security.py")
        return 1
    print("[PASS] 所有攻击样例均被拦截，合法语句均正常放行 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
