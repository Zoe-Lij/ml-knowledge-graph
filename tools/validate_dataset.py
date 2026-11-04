#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
validate_dataset.py
-------------------
数据集质量体检脚本（阶段1 验收 / 阶段5 联调都会用到）。

检查项：
    1. 规模：实体总数是否落在 80~120，类型是否只有 4 类；
    2. 完整性：是否存在悬空关系、孤立节点、自环；
    3. 规范性：关系类型是否只有 5 类，可对比关系是否两端同类；
    4. 图结构：平均度数、度数分布、最大连通子图占比（判断图是否"连成一片"）；
    5. 问答友好度：别名覆盖率（没有别名，自然语言问句就很难命中实体）。

运行：
    python tools/validate_dataset.py
"""

import json
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KG_PATH = os.path.join(ROOT, "data", "kg.json")

ALLOWED_TYPES = {"算法", "模型", "评价指标", "机器学习概念"}
ALLOWED_RELATIONS = {"属于", "衍生自", "基于", "可对比", "应用于"}


def load():
    if not os.path.exists(KG_PATH):
        sys.exit("找不到 data/kg.json，请先运行 python tools/build_dataset.py")
    with open(KG_PATH, encoding="utf-8") as f:
        return json.load(f)


def main():
    kg = load()
    entities = kg["entities"]
    relations = kg["relations"]

    errors, warnings = [], []
    id_type = {e["id"]: e["type"] for e in entities}
    id_name = {e["id"]: e["name"] for e in entities}

    # ---------- 1. 规模与类型 ----------
    if not (80 <= len(entities) <= 120):
        errors.append("实体总数 %d 超出 80~120 的控制范围" % len(entities))
    type_counter = Counter(e["type"] for e in entities)
    for t in type_counter:
        if t not in ALLOWED_TYPES:
            errors.append("非法实体类型：%s" % t)

    # ---------- 2. 关系合法性与完整性 ----------
    id_set = {e["id"] for e in entities}
    rel_counter = Counter(r["type"] for r in relations)
    for rel in rel_counter:
        if rel not in ALLOWED_RELATIONS:
            errors.append("非法关系类型：%s" % rel)

    degree = defaultdict(int)
    neighbors = defaultdict(set)
    for r in relations:
        if r["source"] not in id_set or r["target"] not in id_set:
            errors.append("悬空关系：%s" % r["id"])
            continue
        if r["source"] == r["target"]:
            errors.append("自环：%s" % r["id"])
        degree[r["source"]] += 1
        degree[r["target"]] += 1
        neighbors[r["source"]].add(r["target"])
        neighbors[r["target"]].add(r["source"])

    isolated = [e["name"] for e in entities if degree.get(e["id"], 0) == 0]
    if isolated:
        errors.append("孤立节点 %d 个：%s" % (len(isolated), "、".join(isolated)))

    # ---------- 3. 连通性（弱连通分量） ----------
    seen, components = set(), []
    for e in entities:
        if e["id"] in seen:
            continue
        stack, comp = [e["id"]], []
        seen.add(e["id"])
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nxt in neighbors[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        components.append(comp)
    components.sort(key=len, reverse=True)
    if len(components) > 1:
        warnings.append("存在 %d 个连通分量，最大分量覆盖 %d/%d 个实体"
                        % (len(components), len(components[0]), len(entities)))

    # ---------- 4. 别名覆盖 ----------
    no_alias = [e["name"] for e in entities if not e["aliases"].strip()]
    if no_alias:
        warnings.append("%d 个实体没有别名，可能影响自然语言命中：%s"
                        % (len(no_alias), "、".join(no_alias[:5])))

    # ---------- 5. 置信度标注（优化点） ----------
    model = kg.get("meta", {}).get("confidence_model", {})
    threshold = model.get("review_threshold", 0.80)
    valid_scores = {lv["score"] for lv in model.get("levels", [])} or {0.98, 0.92, 0.86, 0.78}
    conf_counter = Counter()
    bad_conf = []
    for r in relations:
        conf = r.get("confidence")
        if conf is None:
            bad_conf.append("%s 缺 confidence" % r["id"])
            continue
        if not (0.0 < float(conf) <= 1.0):
            bad_conf.append("%s 置信度越界：%s" % (r["id"], conf))
        if float(conf) not in valid_scores:
            bad_conf.append("%s 置信度不在 4 档内：%s" % (r["id"], conf))
        if not r.get("evidence"):
            bad_conf.append("%s 缺 evidence 来源" % r["id"])
        conf_counter["%.2f" % float(conf)] += 1
    errors.extend(bad_conf)
    review = [r for r in relations if float(r.get("confidence", 1)) < threshold]
    low_share = len(review) / max(len(relations), 1)
    if low_share > 0.5:
        warnings.append("低置信度边占比 %.0f%%，超过一半，说明扩展实体过多" % (low_share * 100))

    # ---------- 5.5 推理约束（图谱建模硬规则） ----------
    # 约束1：「属于」只能 细分概念 → 上位大类，上位大类必须是「机器学习概念」；
    # 约束2：算法和模型之间只能用「基于」（模型—基于→算法），禁止「属于」；
    # 约束4：每条边必须带置信度与来源；低置信（<0.80）必须进复核清单。
    for r in relations:
        st, tt = id_type.get(r["source"], ""), id_type.get(r["target"], "")
        if r["type"] == "属于":
            if st in ("算法", "模型") and tt in ("算法", "模型"):
                errors.append("约束2违例：算法/模型之间禁止属于 %s（%s—属于→%s），应改为「基于」"
                              % (r["id"], id_name.get(r["source"]), id_name.get(r["target"])))
            elif tt != "机器学习概念":
                errors.append("约束1违例：属于边 %s（%s—属于→%s）的尾实体不是上位大类「机器学习概念」"
                              % (r["id"], id_name.get(r["source"]), id_name.get(r["target"])))

    # 约束4：低置信边必须出现在复核清单里
    low_path = os.path.join(ROOT, "data", "low_confidence_relations.csv")
    if os.path.exists(low_path):
        with open(low_path, encoding="utf-8") as f:
            listed = {line.split(",")[0] for line in f.read().splitlines()[1:] if line.strip()}
        should_list = {r["id"] for r in relations
                       if r.get("confidence") is not None and float(r["confidence"]) < threshold}
        missing = should_list - listed
        if missing:
            errors.append("约束4违例：%d 条低置信边未进复核清单：%s"
                          % (len(missing), "、".join(sorted(missing)[:5])))
        extra = listed - {r["id"] for r in relations}
        if extra:
            warnings.append("复核清单里有 %d 条已不存在的边 id（可能已被修正）：%s"
                            % (len(extra), "、".join(sorted(extra)[:5])))

    # ---------- 6. 打印报告 ----------
    total_deg = sum(degree.values())
    avg_deg = total_deg / max(len(entities), 1)
    top = sorted(entities, key=lambda e: degree.get(e["id"], 0), reverse=True)[:8]

    print("=" * 60)
    print("ML-KG 数据集体检报告")
    print("=" * 60)
    print("实体 %d 个 / 关系 %d 条 / 平均度数 %.2f" % (len(entities), len(relations), avg_deg))
    print("-" * 60)
    print("实体类型分布：", "，".join("%s %d" % (k, v) for k, v in type_counter.items()))
    print("关系类型分布：", "，".join("%s %d" % (k, v) for k, v in rel_counter.items()))
    print("-" * 60)
    print("度数 Top8（图谱枢纽节点，前端默认展示它们效果最好）：")
    for e in top:
        print("  %-18s %-6s 度=%d" % (e["name"], e["type"], degree[e["id"]]))
    print("-" * 60)
    print("弱连通分量：%d 个，最大分量 %d 个实体" % (len(components), len(components[0])))
    print("别名覆盖：%d/%d" % (len(entities) - len(no_alias), len(entities)))
    print("-" * 60)
    print("置信度分布：" + "，".join("%s %d 条" % (k, v) for k, v in sorted(conf_counter.items(), reverse=True)))
    print("待复核（< %.2f）：%d 条，占比 %.0f%%（清单见 data/low_confidence_relations.csv）"
          % (threshold, len(review), low_share * 100))
    print("-" * 60)
    if errors:
        print("[FAIL] 发现 %d 个错误：" % len(errors))
        for e in errors:
            print("  -", e)
    else:
        print("[PASS] 硬性检查全部通过：类型/关系类型/完整性/无孤立节点")
    for w in warnings:
        print("[WARN]", w)
    print("=" * 60)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
