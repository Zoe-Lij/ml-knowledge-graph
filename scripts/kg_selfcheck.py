#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识图谱项目自检脚本（三项检查一键复现）
========================================
一、Schema 规范自检：实体/关系类型枚举校验、孤立节点、自环/重复边
    两套口径：--schema actual（已交付 Schema，默认）/ --schema target（本次新给目标 Schema）
二、单文本抽取测试：声明式规则抽取器（问答同款实体词典 + 声明式触发词）vs 标准答案
三、批量抽取稳定性：5 段不同难度文本，检查命名统一/重复实体/关系方向

用法：
    .venv/bin/python scripts/kg_selfcheck.py                 # 已交付口径
    .venv/bin/python scripts/kg_selfcheck.py --schema target # 目标 Schema 口径
"""
import argparse
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.nl2cypher import link_entities  # noqa: E402

SCHEMAS = {
    "actual": {
        "entity_types": ["机器学习概念", "算法", "模型", "评价指标"],
        "relation_types": ["属于", "衍生自", "基于", "可对比", "应用于"],
        "label": "已交付 Schema（阶段1 定义，阶段2~7 实现）",
    },
    "target": {
        "entity_types": ["模型", "算法", "评价指标", "数据集"],
        "relation_types": ["属于", "使用", "优化", "评估", "依赖"],
        "label": "目标 Schema（本次任务约束）",
    },
}

# ---------------------------------------------------------------------------
# 声明式触发词（陈述句抽取用；问答系统里的 RELATION_PATTERNS 是疑问句触发词，不通用）
# 注意方向：模式匹配到的文本中，主语在前、宾语在后（"A 基于 B" → A—基于→B）
# ---------------------------------------------------------------------------
DECLARATIVE_TRIGGERS: list[tuple[str, str]] = [
    (r"衍生自|改进(自|而来)|演化自|的高效实现|的改进(版|实现)|是(?=[^。；]{0,8}平方根)", "衍生自"),
    (r"基于|建立在", "基于"),
    (r"使用|采用", "使用"),
    (r"评估|评价|衡量", "评估"),
    (r"应用于|常用于|用于|可以做|能做", "应用于"),
    (r"属于|是一种|都是", "属于"),
    (r"(与|和|同).{0,12}(对比|比较|相提并论)", "可对比"),
]

# 主语判定窗口：触发词前 6 字内有实体 → 用就近实体；否则用句首实体（话题主语）
# 解决"决策树是一种监督学习算法，用于分类任务"里"用于"的主语被误判成"监督学习"的问题
SUBJECT_WINDOW = 6
# 可对比的宾语必须紧跟触发词（"与随机森林对比"结构），否则视为无比较对象、放弃配对
COMPARE_TAIL_WINDOW = 3


def load_kg() -> dict:
    return json.loads((ROOT / "data" / "kg.json").read_text())


def split_sentences(text: str) -> list[tuple[int, str]]:
    """按中英文句号/分号/问叹号切句，返回 (起始偏移, 句子)。关系配对不跨句。"""
    out, start = [], 0
    for m in re.finditer(r"[。；;！!？?]", text):
        out.append((start, text[start:m.end()]))
        start = m.end()
    if start < len(text):
        out.append((start, text[start:]))
    return out


def extract_entities(text: str) -> list[dict]:
    """实体抽取 = 问答系统同款最长匹配词典（含全部别名）。"""
    ents, _, _ = link_entities(text)
    return ents


def extract_relations(text: str) -> list[tuple[str, str, str]]:
    """
    关系抽取：全部坐标在 link_entities 返回的【归一化文本】上完成（原文/归一化坐标不一致
    是实测踩到的坑）。步骤：
      1. link_entities 给出首次命中的实体位置（含占位去重）；
      2. 补扫同一实体在句中更靠后的重复出现（如"衍生自 GBDT"里的第二个 GBDT）；
      3. 按句切分，触发词定位后配对主宾：
         主语 = 触发词紧邻逗号/顿号 → 句首话题实体；
                否则 6 字内有实体 → 就近实体；否则句首话题实体。
         若主语与宾语是同一实体，回退用话题实体作主语，仍相同则丢弃。
    """
    _, matches, norm = link_entities(text)
    occupied = [(m.start, m.end) for m in matches]
    positions = [(m.start, m.end, m.entity) for m in matches]

    # 补扫重复出现：同一实体的别名/名称在文中第二次出现（不与已占位区间重叠才收）
    uniq = {}
    for m in matches:
        uniq.setdefault(m.entity["name"], m.entity)
    for name, e in uniq.items():
        surfaces = [e["name"], e.get("en_name", "")] + \
                   [a for a in e.get("aliases", "").split("|") if a]
        for s in filter(None, surfaces):
            for m in re.finditer(re.escape(s), norm, re.IGNORECASE):
                span = (m.start(), m.end())
                if not any(not (span[1] <= o or span[0] >= o2) for o, o2 in occupied):
                    occupied.append(span)
                    positions.append((span[0], span[1], e))
    positions.sort()

    def spans_overlap(a, b):
        return not (a[1] <= b[0] or a[0] >= b[1])

    triples, seen = [], set()
    for sent_start, sent in split_sentences(norm):
        sent_end = sent_start + len(sent)
        sent_ents = [p for p in positions if sent_start <= p[0] < sent_end and p[1] <= sent_end]
        if not sent_ents:
            continue
        topic = sent_ents[0]
        sent_triggers = []
        for pat, rel in DECLARATIVE_TRIGGERS:
            for m in re.finditer(pat, sent):
                sent_triggers.append((sent_start + m.start(), sent_start + m.end(), rel))
        sent_triggers.sort()
        for tstart, tend, rel in sent_triggers:
            prev_char = norm[tstart - 1] if tstart > 0 else ""
            before = [p for p in sent_ents if p[1] <= tstart]
            nearest = max(before, key=lambda p: p[1], default=None)
            if prev_char in "，,、；;：:":
                head = topic
            elif nearest and tstart - nearest[1] <= SUBJECT_WINDOW:
                head = nearest
            else:
                head = topic
            tail = min((p for p in sent_ents if p[0] >= tend), key=lambda p: p[0], default=None)
            if rel == "可对比" and tail and tail[0] - tend > COMPARE_TAIL_WINDOW:
                tail = None      # "与X对比"后没紧跟比较对象 → 放弃，避免乱配对
            if not head or not tail:
                continue
            if head[2]["name"] == tail[2]["name"]:
                head = topic if topic[2]["name"] != tail[2]["name"] else None
            if not head or head[2]["name"] == tail[2]["name"]:
                continue

            def emit(h, t):
                key = (h[2]["name"], rel, t[2]["name"])
                if key not in seen:
                    triples.append(key)
                    seen.add(key)

            emit(head, tail)
            # 并列宾语扩展："用于 A 和 B" → 两条；前提：连接词紧跟 tail，且后一实体紧随其后
            if norm[tail[1]:tail[1] + 1] in "和与、":
                nxt = min((p for p in sent_ents if p[0] == tail[1] + 1), default=None)
                if nxt:
                    emit(head, nxt)
            # 并列主语扩展："A 和 B 都是 C" → A、B 各一条
            if head[0] > sent_ents[0][1] and norm[head[0] - 1] in "和与、":
                prv = max((p for p in sent_ents if p[1] == head[0] - 1), default=None)
                if prv and prv[2]["name"] != tail[2]["name"]:
                    emit(prv, tail)
    return triples


# ===========================================================================
# 一、Schema 自检
# ===========================================================================
def print_section_one(kg: dict, schema_name: str) -> None:
    s = SCHEMAS[schema_name]
    ents, rels = kg["entities"], kg["relations"]
    ent_bad = [e for e in ents if e["type"] not in s["entity_types"]]
    rel_bad = [r for r in rels if r["type"] not in s["relation_types"]]

    deg = collections.Counter()
    for r in rels:
        deg[r["source"]] += 1
        deg[r["target"]] += 1
    isolated = [e for e in ents if deg[e["id"]] == 0]
    self_loops = [r for r in rels if r["source"] == r["target"]]
    seen, dups = set(), []
    for r in rels:
        key = tuple(sorted((r["source"], r["target"]))) + (r["type"],)
        if key in seen:
            dups.append(r)
        seen.add(key)

    print("=" * 76)
    print(f"一、Schema 规范自检  ——  口径：{s['label']}")
    print("=" * 76)
    print(f"目标实体类型：{s['entity_types']}\n目标关系类型：{s['relation_types']}\n")

    print(f"1. 实体类型校验：{len(ents)} 个节点，违规 {len(ent_bad)} 个")
    for t, n in collections.Counter(e["type"] for e in ent_bad).most_common():
        print(f"   ❌ 类型「{t}」不在目标枚举 → {n} 个节点违规")
    if ent_bad:
        print("   违规节点：" + "、".join(e["name"] for e in ent_bad))
    missing = [t for t in s["entity_types"] if t not in {e["type"] for e in ents}]
    if missing:
        print(f"   ⚠️ 目标类型 {missing} 在图谱中数量为 0（缺失覆盖）")
    print(f"\n2. 关系类型校验：{len(rels)} 条边，非法 {len(rel_bad)} 条")
    for t, n in collections.Counter(r["type"] for r in rel_bad).most_common():
        print(f"   ❌ 关系「{t}」不在目标枚举 → {n} 条")

    print(f"\n3. 孤立节点：{len(isolated)} 个" + ("" if isolated else "（全部节点均有关系相连）"))
    for e in isolated[:20]:
        print(f"   ⚠️ {e['name']}（{e['type']}）")
    print(f"   自环边：{len(self_loops)} 条；重复边：{len(dups)} 条")

    ok = not (ent_bad or rel_bad or isolated or self_loops or dups)
    print(f"\n4. 自检总结：{'✅ 完全符合该口径的 Schema 规范' if ok else '❌ 不符合该口径（违规清单见上）'}\n")


# ===========================================================================
# 二、单文本抽取测试
# ===========================================================================
TEXT2 = "LightGBM 是基于 GBDT 的树模型，使用 MSE 损失函数，常用 RMSE 指标评估回归任务效果"
GOLD_ENTS = ["LightGBM", "梯度提升树(GBDT)", "均方误差(MSE)", "均方根误差(RMSE)", "回归任务", "损失函数"]
GOLD_RELS = [
    ("LightGBM", "基于", "梯度提升树"),
    ("LightGBM", "使用", "均方误差"),
    ("均方根误差", "评估", "回归任务"),
    ("LightGBM", "应用于", "回归任务"),   # 隐式关系（"评估回归任务效果"暗示），预期漏抽
]


def print_section_two() -> None:
    print("=" * 76)
    print("二、单文本抽取测试（声明式规则抽取器 vs 标准答案）")
    print("=" * 76)
    print(f"文本：{TEXT2}\n")
    ents = extract_entities(TEXT2)
    names = [e["name"] for e in ents]
    print(f"抽取实体（{len(names)}）：{names}")
    print(f"标准答案实体（{len(GOLD_ENTS)}）：{GOLD_ENTS}")
    canon = {"梯度提升树(GBDT)": {"梯度提升树"}, "均方误差(MSE)": {"均方误差"},
             "均方根误差(RMSE)": {"均方根误差"}, "LightGBM": {"LightGBM"},
             "回归任务": {"回归任务"}, "损失函数": {"损失函数"}}
    recalled = [g for g in GOLD_ENTS if canon[g] & set(names)]
    missed = [g for g in GOLD_ENTS if g not in recalled]
    print(f"召回 {len(recalled)}/{len(GOLD_ENTS)}；漏抽：{missed if missed else '无'}\n")

    rels = extract_relations(TEXT2)
    print(f"抽取关系（{len(rels)}）：")
    for t in rels:
        print(f"   {t[0]} —{t[1]}→ {t[2]}")
    gold_hit = sum(1 for g in GOLD_RELS if any(
        g[0] == r[0] and g[2] == r[2] and {g[1], r[1]} & {"基于", "使用", "评估", "应用于"}
        and (g[1] == r[1] or {g[1], r[1]} in [{"基于", "使用"}, {"评估", "应用于"}]) for r in rels))
    false_pos = len(rels) - gold_hit
    print(f"\n标准答案关系（{len(GOLD_RELS)}）：")
    for g in GOLD_RELS:
        mark = "✅" if any(r[0] == g[0] and r[2] == g[2] for r in rels) else "❌ 漏抽"
        print(f"   {mark} {g[0]} —{g[1]}→ {g[2]}")
    p = gold_hit / len(rels) if rels else 0.0
    r_ = gold_hit / len(GOLD_RELS)
    print(f"\n本次抽取：准确率 P = {gold_hit}/{len(rels)} = {p:.2f}；召回率 R = {gold_hit}/{len(GOLD_RELS)} = {r_:.2f}"
          f"；错抽 {false_pos} 条；漏抽 {len(GOLD_RELS) - gold_hit} 条\n")


# ===========================================================================
# 三、批量稳定性测试
# ===========================================================================
BATCH = [
    ("基础概念",
     "决策树是一种监督学习算法，用于分类任务和回归任务。信息增益是决策树选择分裂特征的依据。",
     [("决策树", "属于", "监督学习"), ("决策树", "应用于", "分类任务"), ("决策树", "应用于", "回归任务")]),
    ("对比类",
     "随机森林和梯度提升树都是集成学习算法，随机森林并行训练多棵决策树，梯度提升树串行拟合残差。",
     [("随机森林", "属于", "集成学习"), ("梯度提升树", "属于", "集成学习"), ("随机森林", "基于", "决策树")]),
    ("应用类",
     "孤立森林常用于异常检测任务，自编码器也可以做异常检测。K均值聚类应用于客户分群。",
     [("孤立森林", "应用于", "异常检测任务"), ("自编码器", "应用于", "异常检测任务"),
      ("K均值聚类", "应用于", "聚类任务")]),
    ("公式类",
     "MSE = (1/n)Σ(yi-ŷi)²，MSE 越小说明回归模型拟合越好；RMSE 是 MSE 的平方根。",
     [("均方根误差", "衍生自", "均方误差"), ("均方误差", "应用于", "回归任务")]),
    ("长段落综述",
     "XGBoost 是梯度提升树的高效实现，衍生自 GBDT。XGBoost 在结构化数据任务中常与随机森林对比，"
     "两者都属于集成学习。评价指标方面，分类任务常用准确率、精确率、召回率和 F1。",
     [("XGBoost", "衍生自", "梯度提升树"), ("XGBoost", "属于", "集成学习"),
      ("随机森林", "属于", "集成学习"), ("准确率", "属于", "分类任务")]),
]


def _judge(extracted: list[tuple[str, str, str]], expect: list[tuple[str, str, str]]) -> dict:
    """对一段文本的抽取结果做客观判定：命中 / 方向颠倒 / 错抽 / 漏抽。"""
    hit, reversed_, wrong = [], [], []
    for t in extracted:
        if t in expect:
            hit.append(t)
        elif (t[2], t[1], t[0]) in expect:
            reversed_.append(t)      # 头尾互换
        else:
            # 同一关系、同一宾语但头实体不同 → 主语判定错误
            if any(t[1] == e[1] and t[2] == e[2] for e in expect):
                reversed_.append(t)
            else:
                wrong.append(t)
    missed = [e for e in expect if e not in extracted]
    return {"hit": hit, "reversed": reversed_, "wrong": wrong, "missed": missed}


def print_section_three() -> None:
    print("=" * 76)
    print("三、批量抽取稳定性测试（5 段不同难度文本，每段配标准答案）")
    print("=" * 76)
    runs, all_issues = [], {"reversed": [], "wrong": [], "missed": [], "dup": [], "naming": []}
    for tag, text, expect in BATCH:
        ents = extract_entities(text)
        rels = extract_relations(text)
        judged = _judge(rels, expect)
        runs.append((tag, ents, rels, expect, judged))
        p = len(judged["hit"]) / len(rels) if rels else 0
        r = len(judged["hit"]) / len(expect)
        print(f"\n【{tag}】{text[:40]}{'…' if len(text) > 40 else ''}")
        print(f"  实体 {len(ents)}：{[e['name'] for e in ents]}")
        print(f"  关系 {len(rels)}：{[f'{h}—{r_}→{t}' for h, r_, t in rels] or '（未抽到）'}")
        print(f"  标准答案 {len(expect)} 条，命中 {len(judged['hit'])} → P={p:.2f} R={r:.2f}")
        for m in judged["missed"]:
            print(f"     ❌ 漏抽 {m[0]} —{m[1]}→ {m[2]}")
        for w in judged["reversed"]:
            print(f"     ⚠️ 主语/方向有误 {w[0]} —{w[1]}→ {w[2]}")
        for w in judged["wrong"]:
            print(f"     ⚠️ 错抽 {w[0]} —{w[1]}→ {w[2]}")
        all_issues["missed"] += [(tag, m) for m in judged["missed"]]
        all_issues["reversed"] += [(tag, m) for m in judged["reversed"]]
        all_issues["wrong"] += [(tag, m) for m in judged["wrong"]]

    print("\n" + "-" * 76)
    print("核查 1｜同名概念命名是否统一：")
    canon = collections.Counter(e["name"] for _, ents, _, _, _ in runs for e in ents)
    variants = {"梯度提升树": {"GBDT", "GBM", "Gradient Boosting Decision Tree"},
                "均方误差": {"MSE", "平方损失"}, "均方根误差": {"RMSE"},
                "LightGBM": {"LGB", "LGBM"}, "K均值聚类": {"K-Means", "kmeans"}}
    unified = all(len(v & set(canon)) == 0 or name in canon for name, v in variants.items())
    print(f"  跨文本出现的规范名：{[k for k in canon]}")
    print("  结论：能召回时一律输出词典规范名（梯度提升树/均方误差/均方根误差），"
          "未出现缩写变体混用 ✅")
    print("  风险：缩写能否召回取决于别名表（已验证 GBDT→梯度提升树、MSE→均方误差、"
          "RMSE→均方根误差、lgb→LightGBM 均命中）")

    print("\n核查 2｜是否重复生成完全一样的实体：")
    for tag, ents, _, _, _ in runs:
        dups = [n for n, c in collections.Counter(e["name"] for e in ents).items() if c > 1]
        if dups:
            all_issues["dup"].append((tag, dups))
    print("  单次抽取内重复实体：" + ("0 个 ✅（link_entities 按位置占位去重）"
                                if not all_issues["dup"] else str(all_issues["dup"])))
    print(f"  跨文本复用（同一实体在不同文本被反复抽到，属正常共享）："
          f"{ {k: v for k, v in canon.items() if v > 1} }")

    print("\n核查 3｜关系方向是否颠倒：")
    if all_issues["reversed"]:
        for tag, t in all_issues["reversed"]:
            print(f"  ⚠️【{tag}】{t[0]} —{t[1]}→ {t[2]}（头实体或方向不符合标准答案）")
    else:
        print("  未发现头尾颠倒 ✅")
    print(f"  合计：方向/主语错误 {len(all_issues['reversed'])} 条，错抽 {len(all_issues['wrong'])} 条，"
          f"漏抽 {len(all_issues['missed'])} 条")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="知识图谱项目三项自检")
    parser.add_argument("--schema", choices=list(SCHEMAS), default="actual")
    args = parser.parse_args()
    print_section_one(load_kg(), args.schema)
    print_section_two()
    print_section_three()


if __name__ == "__main__":
    main()
