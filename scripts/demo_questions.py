#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Demo 演示问句自检脚本
====================
上台前跑一遍，确认所有演示问句都能正常回答（离线解析 + 在线接口双重校验）。

用法：
    python scripts/demo_questions.py                        # 离线模式：只验证意图解析（不需要服务）
    python scripts/demo_questions.py --base-url http://127.0.0.1:8000   # 在线模式：连服务实测
"""
import argparse
import json
import sys
import urllib.request

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from app.nl2cypher import build_plan  # noqa: E402

# ---------------------------------------------------------------------------
# 演示问句清单：(问句, 期望意图, 回答中必须出现的关键词)
# 说明：keywords 任一命中即算 PASS，用于防止"意图对但答案跑偏"。
# ---------------------------------------------------------------------------
DEMO_QUESTIONS = [
    # —— 用户指定的 5 句 ——
    ("随机森林衍生自什么模型？",        "neighbors", ["衍生自", "决策树"]),
    ("MSE适用于什么机器学习任务？",     "neighbors", ["均方误差", "回归任务"]),
    ("SVM和逻辑回归是否可以对比？",     "compare",   ["可对比", "支持向量机"]),
    ("监督学习包含哪些算法？",          "filter",    ["支持向量机", "决策树"]),
    ("聚类属于哪一类机器学习？",        "neighbors", ["聚类任务", "无监督学习"]),
    # —— 答辩脚本里的备用问句 ——
    ("SVM和逻辑回归有什么关系？",       "path",      ["1 跳", "可对比"]),
    ("随机森林是基于什么模型衍生的？",  "neighbors", ["衍生自", "决策树"]),
    ("随机森林和梯度提升树有什么区别？", "compare",  ["可对比", "共同关联知识点"]),
    ("随机森林和K均值聚类怎么连起来？", "path",      ["4 跳", "集成学习"]),
    ("哪些算法可以做异常检测？",        "filter",    ["孤立森林", "单类SVM"]),
    ("聚类和分类有什么区别？",          "compare",   ["聚类任务", "分类任务"]),
    ("什么是过拟合？",                  "detail",    ["过拟合"]),
]


def check_offline() -> list[tuple[str, str, str]]:
    """离线校验：意图解析是否正确。返回 (问句, 状态, 说明)。"""
    results = []
    for q, want, _ in DEMO_QUESTIONS:
        try:
            plan = build_plan(q)
            ok = plan.kind == want
            detail = f"intent={plan.kind}（期望 {want}）"
            results.append((q, "PASS" if ok else "FAIL", detail))
        except Exception as exc:  # noqa: BLE001
            results.append((q, "FAIL", f"解析异常: {exc}"))
    return results


def check_online(base_url: str) -> list[tuple[str, str, str]]:
    """在线校验：真实接口返回 + 关键词断言。"""
    results = []
    for q, want, keywords in DEMO_QUESTIONS:
        try:
            req = urllib.request.Request(
                f"{base_url.rstrip('/')}/api/query",
                data=json.dumps({"question": q}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            answer = data.get("answer") or ""
            problems = []
            if data.get("intent") != want:
                problems.append(f"intent={data.get('intent')}（期望 {want}）")
            if not any(k in answer for k in keywords):
                problems.append(f"回答缺少关键词 {keywords}")
            results.append((q, "FAIL" if problems else "PASS",
                            "；".join(problems) or answer[:46] + "…"))
        except Exception as exc:  # noqa: BLE001
            results.append((q, "FAIL", f"请求异常: {exc}"))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Demo 演示问句自检")
    parser.add_argument("--base-url", default=None, help="传入则额外做在线校验")
    args = parser.parse_args()

    rows = check_offline()
    mode = "离线模式（意图解析）"
    if args.base_url:
        rows = check_online(args.base_url)
        mode = f"在线模式（{args.base_url}）"

    print("=" * 72)
    print(f"Demo 演示问句自检 · {mode} · 共 {len(rows)} 句")
    print("=" * 72)
    for q, status, detail in rows:
        mark = "✅" if status == "PASS" else "❌"
        print(f"{mark} [{status}] {q}\n     └─ {detail}")
    failed = sum(1 for _, s, _ in rows if s != "PASS")
    print("-" * 72)
    print(f"汇总：通过 {len(rows) - failed} 项，失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
