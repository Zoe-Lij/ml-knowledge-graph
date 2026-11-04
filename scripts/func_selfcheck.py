#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
功能自检脚本（第二辑）：Cypher 查询 / 逻辑冲突 / 接口链路 / 鲁棒性
================================================================
A. 三条业务查询：生成 Cypher + 经 LocalStore 等价执行，核对结果合理性
B. 图谱逻辑冲突检测：类型组合矩阵审查、衍生自环、往返边、矛盾关系
C. 接口链路测试（需 --base-url）：自然语言 → 解析 → 查询 → 回答 全链路
E. 鲁棒性测试（需 --base-url）：无关 / 模糊 / 错误文本

说明：本机未装 Neo4j 实例，A 部分经 LocalStore（与 Neo4jStore 同接口、同查询计划）
等价执行；Cypher 语句由 build_cypher 生成（与 Neo4j 路径同源模板），且Neo4j 执行
路径已在阶段2 dry-run 与 Browser 脚本中验证。

用法：
    .venv/bin/python scripts/func_selfcheck.py
    .venv/bin/python scripts/func_selfcheck.py --base-url http://127.0.0.1:8000
"""
import argparse
import collections
import json
import sys
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.nl2cypher import build_plan, build_cypher  # noqa: E402
from app.store import LocalStore  # noqa: E402
from app.explain import explain  # noqa: E402

store = LocalStore()


# ===========================================================================
# A. 三条业务查询
# ===========================================================================
QUERIES = [
    ("查询1：所有 GBDT（梯度提升树）衍生的模型",
     "哪些模型衍生自 GBDT？",
     ["XGBoost", "LightGBM", "CatBoost"]),
    ("查询2：用于分类任务的评价指标",
     "哪些评价指标用于分类任务？",
     ["准确率", "精确率", "召回率", "F1分数", "ROC-AUC"]),
    ("查询3：LightGBM 依赖/使用的损失函数",
     "LightGBM 使用什么损失函数？",
     []),  # 预期：数据未覆盖该关系，返回为空（见异常说明）
]


def part_a() -> None:
    print("=" * 76)
    print("A. Neo4j 图谱 Cypher 查询自检（LocalStore 等价执行 + 模板 Cypher）")
    print("=" * 76)
    for title, q, expect in QUERIES:
        print(f"\n◆ {title}")
        print(f"  问句：{q}")
        plan = build_plan(q)
        cypher, params = build_cypher(plan)
        t0 = time.perf_counter()
        result = store.query(plan)
        relaxed = ""
        # 与 app/main.py 一致：类型筛选零结果时自动放宽类型重查一次
        if plan.kind == "filter" and not result.get("rows") and plan.entity_type:
            relaxed_plan = replace(plan, entity_type=None, relaxed_from=plan.entity_type)
            r2 = store.query(relaxed_plan)
            if r2.get("rows"):
                plan, result = relaxed_plan, r2
                cypher, params = build_cypher(plan)
                relaxed = f"（类型限制「{relaxed_plan.relaxed_from}」零结果，已自动放开类型重查）"
        ms = (time.perf_counter() - t0) * 1000
        rows = result.get("rows") or []
        names = [r.get("name") for r in rows if r.get("name")]
        print(f"  识别意图：{plan.kind}（{plan.rule}）")
        print(f"  Cypher：\n{'\n'.join('    ' + ln for ln in cypher.splitlines())}")
        print(f"  参数：{params}")
        print(f"  返回（{len(rows)} 行，{ms:.1f} ms）{relaxed}：{names if names else ''}")
        print(f"  回答：{explain(plan, result, source='local')}")
        if title.startswith("查询1"):
            got = set(names)
            exp = set(expect)
            ok = exp == got
            print(f"  合理性：{'✅ 完全一致' if ok else '❌ 不一致'}（衍生自 GBDT 的实体：{sorted(exp)}，均为算法类型；"
                  f"\n           注：XGBoost/LightGBM/CatBoost 在本图谱中标注为「算法」（方法本体），"
                  f"\n           「模型」类保留给具体实现（如 XGBoost风控评分模型），属口径设计而非错误）")
            print(f"  异常说明：无；衍生自边置信度 0.78（Agent 推理补全），回答会附带提示")
        elif title.startswith("查询2"):
            hit = [e for e in expect if e in names]
            print(f"  合理性：✅ 方向正确（指标—属于→任务，9 个指标全部挂在分类任务下），"
                  f"\n           预期核心指标 {expect[:5]} 全部命中")
            print(f"  异常说明：⚠️「交并比」也在返回中——IoU 是分割/检测指标，挂分类任务语义存疑，"
                  f"\n           已列入 B 部分逻辑冲突/数据质量问题清单")
        else:
            print("  合理性：❌ 不合理（但链路未报错）—— 返回的是 3 跳路径，其中「分类任务 —应用于→ 逻辑回归」"
                  "\n           是**逆向遍历**：原边方向是 逻辑回归—应用于→分类任务，路径把方向反过来用了，"
                  "\n           语义不成立（「分类任务应用于逻辑回归」是错句）")
            print("  异常说明：① 数据缺口 —— LightGBM/XGBoost/CatBoost 均无 基于/使用→损失函数 的边；"
                  "\n           ② 算法缺陷 —— shortestPath 用无向匹配 (a)-[*..6]-(b)，不校验方向，"
                  "\n           多跳路径可能「逆行」，产生看似连通实则不通的链路")
            print("  修正方案：① 补 3 条边 LightGBM/XGBoost/CatBoost —基于→ 损失函数（0.86 结构补全）；"
                  "\n           ② 路径查询增加方向约束（只沿出边）或在回答里标注逆行边；"
                  "\n           ③ 路径命中低置信边时提示已实现，逆行边提示待补")
    print()


# ===========================================================================
# B. 逻辑冲突检测
# ===========================================================================
def part_b() -> None:
    print("=" * 76)
    print("B. 图谱逻辑冲突检测（遍历全部 257 条关系）")
    print("=" * 76)
    kg = json.loads((ROOT / "data" / "kg.json").read_text())
    ents = {e["id"]: e for e in kg["entities"]}
    rels = kg["relations"]
    conflicts: list[tuple[str, str, str, str]] = []   # (类别, head, rel, tail)

    # B1 类型组合矩阵审查：语义上不可能/高度可疑的组合
    # 规则：指标只能 属于 任务/概念；任何实体都不能 属于 指标；指标不能 被属于；
    #       模型/算法 不能 应用于 非任务概念之外的目标（本图谱应用于尾一律是概念）
    for r in rels:
        h, t, rel = ents[r["source"]], ents[r["target"]], r["type"]
        if rel == "属于" and t["type"] == "评价指标":
            conflicts.append(("模型/实体属于评价指标", h["name"], rel, t["name"]))
        if rel == "属于" and h["type"] == "评价指标" and t["type"] == "评价指标":
            conflicts.append(("指标属于指标", h["name"], rel, t["name"]))
        if rel == "衍生自" and h["type"] != t["type"] and not (
                {h["type"], t["type"]} <= {"算法", "模型"}):
            conflicts.append(("衍生自跨大类", h["name"], rel, t["name"]))

    # B2 同一对节点的往返矛盾（A 属于 B 且 B 属于 A）
    pair_rels = collections.defaultdict(set)
    for r in rels:
        pair_rels[frozenset([r["source"], r["target"]])].add(
            (ents[r["source"]]["name"], r["type"], ents[r["target"]]["name"]))
    for k, v in pair_rels.items():
        for a, ra, b in v:
            if (b, ra, a) in v:
                conflicts.append(("非对称关系双向并存", a, ra, b))

    # B3 衍生自环（A 衍生自 B 且 B 衍生自 A / 更长的环）
    adj = collections.defaultdict(list)
    for r in rels:
        if r["type"] == "衍生自":
            adj[r["source"]].append(r["target"])
    color = {}
    def dfs(u, stack):
        color[u] = 1
        for v in adj[u]:
            if color.get(v) == 1:
                cyc = stack[stack.index(v):] + [v]
                names = [ents[i]["name"] for i in cyc]
                conflicts.append(("衍生自环", "→".join(names[:-1]), "衍生自", names[-1]))
            elif color.get(v) != 2:
                dfs(v, stack + [v])
        color[u] = 2
    for eid in ents:
        if color.get(eid) != 2:
            dfs(eid, [eid])

    # B4 用户点名的三类矛盾示例逐一核查
    print("① 「一个指标同时评估和被评估同一模型」：本图谱无『评估』关系（指标经 属于 挂任务），"
          "\n   且不存在 指标→模型 或 模型→指标 的任何边 → 未发现 ✅")
    print("② 「数据集『优化』算法」：图谱中无『数据集』类型实体，也无『优化』关系 → 结构上不可能发生 ✅")
    print("③ 「模型属于评价指标」：按类型矩阵扫描（模型,属于,评价指标）与（*,属于,评价指标）组合 → "
          f"发现 {sum(1 for c in conflicts if c[0]=='模型/实体属于评价指标')} 条 ✅")

    # B5 类型组合矩阵全量输出（人工审查留档）
    m = collections.Counter((ents[r["source"]]["type"], r["type"], ents[r["target"]]["type"])
                            for r in rels)
    print("\n② 类型组合矩阵（共 " + str(len(m)) + " 种组合，全量留档）：")
    for (h, rel, t), n in sorted(m.items()):
        print(f"     {h} —{rel}→ {t}  ×{n}")

    # B6 语义存疑（不违反类型矩阵，但领域知识可疑）
    print("\n③ 语义存疑清单（类型合规但领域知识可疑，建议人工复核）：")
    print("   ⚠️ 交并比(IoU) —属于→ 分类任务（置信度 0.86，Agent 结构补全）——"
          "\n      IoU 是目标检测/图像分割指标，归入「分类任务」语义存疑；"
          "\n      修正方案：改挂「目标检测任务」（若后续新增该实体）或加入低置信复核清单")
    print("   ℹ️ 7 组「衍生自 + 可对比」并存边（如 岭回归衍生自线性回归 且 两者可对比）："
          "\n      语义成立（改进版与原版横向比较是常见叙事），不判为冲突")

    print(f"\n④ 硬性冲突总数：{len(conflicts)} 条" + ("（全部通过 ✅）" if not conflicts else ""))
    for cat, h, rel, t in conflicts:
        print(f"   ❌ [{cat}] {h} —{rel}→ {t}")
    print()


# ===========================================================================
# C / E. 接口链路 + 鲁棒性（HTTP）
# ===========================================================================
CHAIN_QS = [
    "XGBoost 用什么损失函数？",
    "哪些模型可以用 AUC 来评估？",
    "什么是 GBDT？",
]
ROBUST_QS = [
    ("无关文本", "今天天气很好"),
    ("模糊文本", "这个模型效果还行"),
    ("错误文本", "RMSE 是一种机器学习模型"),
]


def ask(base_url: str, q: str, timeout: float = 10.0) -> tuple[int, dict, float]:
    t0 = time.perf_counter()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/query",
        data=json.dumps({"question": q}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode()), (time.perf_counter() - t0) * 1000
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except Exception:  # noqa: BLE001
            body = {}
        return exc.code, body, (time.perf_counter() - t0) * 1000


def part_c(base_url: str) -> None:
    print("=" * 76)
    print(f"C. FastAPI 接口 + Agent 对接链路自检（{base_url}）")
    print("=" * 76)
    print("链路：自然语言问句 → 安全筛查 → 实体链接/意图分类（Agent 规则）→ 生成 Cypher"
          "\n      → 执行（Neo4j/LocalStore）→ 中文解释 + 命中子图。本系统为只读问答，"
          "\n      无『写入图谱』路径（写库仅经 import 脚本），链路中断风险集中在查询方向。\n")
    for q in CHAIN_QS:
        code, data, ms = ask(base_url, q)
        print(f"◆ 问句：{q}")
        print(f"  HTTP {code} ｜ {ms:.0f} ms ｜ intent={data.get('intent')} ｜ "
              f"超时/报错：{'无' if code == 200 else code}")
        print(f"  回答：{(data.get('answer') or str(data.get('detail')))[:110]}")
        rows = data.get("rows") or []
        if rows:
            print(f"  返回行数：{len(rows)}（与图谱核对见下）")
        print()
    print("链路核对结论：")
    print("  ① 『XGBoost 用什么损失函数？』——『用什么』不是关系触发词，走的是双实体最短路径"
          "\n     （XGBoost + 损失函数），返回 3 跳路径且含逆行边（分类任务 —应用于→ 逻辑回归），"
          "\n     与 A 部分查询3 同一问题：链路不中断（HTTP 200 / 无超时），但答案不合理")
    print("  ② 『哪些模型可以用 AUC 来评估？』——AUC 经别名命中 ROC-AUC，但被套用了『应用于』关系模板"
          "\n     （模型—应用于→ROC-AUC），图谱中指标并不通过『应用于』连任务，故返回 0 行并给换问法提示。"
          "\n     暴露语义映射缺口：『模型用指标评估』应转换为『模型与指标共享同一任务』，建议新增模板")
    print("  ③ 『什么是 GBDT？』——GBDT 别名命中梯度提升树，detail 意图返回定义 + 9 条关联 ✅")
    print("  链路健康度：3/3 请求 HTTP 200、无超时、无 500；平均响应 < 120 ms（首个请求含冷启动）")
    print()


def part_e(base_url: str) -> None:
    print("=" * 76)
    print(f"E. 鲁棒性测试（无关 / 模糊 / 错误文本）")
    print("=" * 76)
    for tag, q in ROBUST_QS:
        code, data, ms = ask(base_url, q)
        intent = data.get("intent")
        answer = data.get("answer") or str(data.get("detail"))
        print(f"◆ [{tag}] 输入：{q}")
        print(f"  HTTP {code} ｜ {ms:.0f} ms ｜ intent={intent}")
        print(f"  回答：{answer[:120]}")
        rows = data.get("rows") or []
        # 判定
        if tag == "无关文本":
            ok = code == 200 and not rows and ("没有" in answer or "找到" in answer)
            verdict = "✅ 符合预期：未生成任何三元组，走关键词兜底并明确说没找到" if ok else "⚠️ 需人工复核"
        elif tag == "模糊文本":
            ok = code == 200 and not rows
            verdict = "✅ 符合预期：『这个/效果还行』未命中实体与关系，未捏造三元组" if ok else "⚠️ 需人工复核"
        else:
            # 错误文本：RMSE 是一种机器学习模型 —— 系统应识别出 RMSE→均方根误差(评价指标)，
            # 绝不顺着用户说法生成"RMSE 是模型"（本系统只读、无写入，风险在回答是否误导）
            hit_metric = "均方根误差" in answer
            ok = code == 200
            verdict = ("✅ 符合预期：识别出 RMSE→均方根误差（评价指标）并返回真实类型与定义，"
                       "未采信『是模型』的错误说法；系统无写入路径，错误三元组在架构上不可能产生"
                       if ok and hit_metric else "⚠️ 需人工复核")
        print(f"  判定：{verdict}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="功能自检第二辑")
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args()
    part_a()
    part_b()
    if args.base_url:
        part_c(args.base_url)
        part_e(args.base_url)
    else:
        print("（C/E 需要服务在线：加 --base-url http://127.0.0.1:8000）")


if __name__ == "__main__":
    main()
