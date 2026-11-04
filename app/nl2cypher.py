#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
nl2cypher.py
-------------
自然语言 → Cypher 的翻译层。整体是"规则 + 模板"，不让大模型自由生成 Cypher。

为什么不用大模型直接生成 Cypher？
    1. 本项目只有 5 种关系，问句类型可以穷举，模板覆盖率高且 100% 可控；
    2. 自由生成会有幻觉（编造不存在的关系类型）和注入风险（生成 DELETE）；
    3. 规则方案可解释：能告诉老师"这句话为什么翻译成这条 Cypher"（plan.rule）。

三步流水线（对应 docs/01_schema_design.md 第 6 节）：
    Step 1 实体识别：用 name + en_name + aliases 构造词典，最长匹配优先；
    Step 2 意图分类：关系触发词 → 关系类型与方向；疑问句式 → 查询模板；
    Step 3 模板填充：把实体名/关系类型塞进预置 Cypher 模板，只传参数，不拼用户输入。

支持的问句类型（10 类）：
    detail     什么是过拟合                 → 实体属性 + 一跳邻居
    neighbors  随机森林属于哪一类           → 一跳关系（含方向）
    filter     哪些算法可以做异常检测       → 类型 + 关系的条件筛选
    compare    随机森林和梯度提升树有什么区别 → 共同邻居 + 直接关系
    path       BERT 和决策树怎么连起来      → shortestPath
    type_list  有哪些评价指标               → 按类型列表
    stats      哪个实体关系最多             → 度数排序
    search     关键词模糊检索               → CONTAINS 兜底
    not_found  没识别出实体                 → 给出建议问法
"""

import re
from dataclasses import asdict, dataclass, field
from difflib import get_close_matches

from .kgdata import RELATION_TYPES, kb

# ---------------------------------------------------------------------------
# 一、问句预处理与实体词典
# ---------------------------------------------------------------------------

# 全角 → 半角（用户从 PPT/Word 复制问句时常见）
_FULLWIDTH = {ord(c): ord(c) - 0xFEE0 for c in "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"}


def normalize(text: str) -> str:
    """统一大小写、去全角、去空白。中文问句里空格是噪声，去掉反而提高召回。"""
    text = text.translate(_FULLWIDTH).lower()
    return re.sub(r"\s+", "", text)


# 泛化词：单独出现时不能当作实体命中，否则"有哪些模型"会把某个叫"xx模型"的实体拉进来
GENERIC_WORDS = {"模型", "算法", "指标", "概念", "任务", "方法", "学习", "分类", "回归", "聚类"}

# 实体类型的中文说法（用于"哪些算法…"这类问句的类型过滤）
TYPE_WORDS = [
    ("评价指标", ["评价指标", "指标", "度量指标", "metric"]),
    ("算法", ["算法", "方法", "algorithm"]),
    ("模型", ["模型", "model"]),
    ("机器学习概念", ["概念", "知识点", "术语"]),
]


def _build_lexicon() -> list[tuple[str, dict]]:
    """
    构造 (归一化表面形式, 实体) 列表，按表面长度降序。
    长度降序是"最长匹配优先"的实现关键：先匹配"随机森林"，就不会再匹配到"森林"。
    """
    items: list[tuple[str, dict]] = []
    for e in kb.entities:
        surfaces = {e["name"], e.get("en_name", "")}
        surfaces |= {a for a in e.get("aliases", "").split("|") if a}
        for s in surfaces:
            s = normalize(s)
            if len(s) < 2:          # 单字符别名（如"熵"）噪声太大，直接丢弃
                continue
            if s in GENERIC_WORDS and s != normalize(e["name"]) and not normalize(e["name"]).startswith(s):
                # 通用词默认不当实体；但若它恰好是实体名的前缀（如"聚类"→聚类任务、"分类"→分类任务），
                # 则是合法别名——用户问"聚类属于哪一类"时必须能命中聚类任务。
                continue
            items.append((s, e))
    items.sort(key=lambda x: len(x[0]), reverse=True)
    return items


LEXICON = _build_lexicon()


@dataclass
class Match:
    """一次实体命中。start/end 是归一化文本中的位置，用于判断出现顺序。"""
    start: int
    end: int
    entity: dict
    surface: str


def link_entities(question: str) -> tuple[list[dict], list[Match], str]:
    """
    实体识别：最长匹配 + 位置不重叠 + 同实体去重。

    返回 (实体列表[按出现顺序], 命中详情, 归一化后的问句)
    """
    text = normalize(question)
    occupied = [False] * len(text)
    matches: list[Match] = []

    for surface, entity in LEXICON:
        start = 0
        while True:
            idx = text.find(surface, start)
            if idx < 0:
                break
            end = idx + len(surface)
            # 该片段没被更长的实体占用，才算有效命中
            if not any(occupied[idx:end]):
                for k in range(idx, end):
                    occupied[k] = True
                matches.append(Match(idx, end, entity, surface))
            start = idx + 1

    matches.sort(key=lambda m: m.start)
    entities: list[dict] = []
    seen: set[str] = set()
    for m in matches:
        if m.entity["name"] not in seen:
            seen.add(m.entity["name"])
            entities.append(m.entity)
    return entities, matches, text


# ---------------------------------------------------------------------------
# 二、关系触发词 → (关系类型, 方向)
# ---------------------------------------------------------------------------
# 方向约定：out = 实体是主语（随机森林 —属于→ 集成学习）
#           in  = 实体是宾语（哪些算法 —属于→ 集成学习）
RELATION_PATTERNS: dict[str, dict[str, list[str]]] = {
    "属于": {
        "out": [r"属于(什么|哪|哪个|哪种|哪类|哪一类)", r"是哪一?类", r"算(什么|哪)", r"归(类|为|属)", r"划分到"],
        "in": [r"(哪些|什么|有啥|有什么|包括|包含|都有|列出).{0,8}属于", r"包括(哪些|什么)", r"包含(哪些|什么)", r"有(哪些|什么)(子类|子任务|分支|下属|下级)"],
    },
    "衍生自": {
        # 最后一条兜底"X 是基于什么模型衍生的"：问句里同时出现"基于"和"衍生"时，
        # 语义重心在"衍生"（问来源），必须由本类优先命中，否则会被"基于"抢走。
        "out": [r"衍生自", r"从.{0,12}(改进|衍生|演化|发展)", r"由.{0,12}(改进|衍生|发展)", r"来源", r"前身", r"基于谁", r"继承自", r"脱胎于", r"(基于|由|从|是).{0,12}衍生"],
        "in": [r"衍生(出|了)", r"改进(出|版|而来)", r"有(哪些|什么)(变体|改进|衍生|升级)", r"发展出", r"演化出"],
    },
    "基于": {
        "out": [r"基于(什么|啥|哪)", r"原理", r"依据", r"核心(思想|思路|假设)", r"为什么(有效|能|可以|work)", r"依赖(什么|于)", r"建立在", r"本质"],
        "in": [r"(哪些|什么|哪种).{0,12}基于", r"基于它", r"建立在.{0,8}之上的是"],
    },
    "应用于": {
        "out": [r"应用(于|在|场景)", r"适(用|合)(于|做|什么)", r"能(做|干|用来|用于|解决|干嘛)", r"可以(做|用来|用于|解决|干)", r"适合(做|用于|什么)", r"用于", r"解决(什么|哪些)", r"用途", r"场景"],
        "in": [r"(哪些|什么|哪种|有啥).{0,12}(可以|能|适合|用于|应用于|做|解决)"],
    },
    "可对比": {
        "out": [r"对比", r"比较", r"区别", r"差异", r"异同", r"哪个好", r"谁更好", r"优劣"],
        "in": [r"对比", r"比较", r"区别", r"差异"],
    },
}

# 关系优先级：问句里同时出现多个触发词时，语义更具体的优先（避免"原理"被"用于"抢走）
RELATION_PRIORITY = ["衍生自", "基于", "应用于", "可对比", "属于"]

# 通用疑问句式
DETAIL_RE = r"(什么是|是啥|什么意思|介绍一下|介绍下|简述|说明一下|是什么)"
LIST_RE = r"(有哪些|有什么|有啥|哪些|列出|全部|所有|清单|包括|包含)"
STATS_RE = r"(最多|最高|最少|统计|数量|多少个|总数|规模|度数|枢纽|最重要)"
COMPARE_RE = r"(区别|差异|不同|对比|比较|异同|哪个好|谁更好|优劣|优缺点|怎么选)"
PATH_RE = r"(怎么(连|联系|关联)|有什么关系|什么关系|路径|如何关联|有联系|关联起来)"

# 列举式问句的"句首"特征："哪些算法…""有什么模型…""哪种指标…"
# 这类问句里，被点名的实体是关系的宾语（哪些算法 —应用于→ 异常检测任务），
# 方向要翻成 in，否则会去查"异常检测任务应用于什么"，结果为空。
LISTING_HEAD_RE = r"^(请问|那么|那)?(图谱中|图里|系统里|整个图里)?(有)?(哪些|什么|哪种|哪几种|哪类|哪个)"


def detect_relation(text: str) -> tuple[str | None, str | None, str]:
    """返回 (关系类型, 方向, 命中的规则描述)。"""
    best: tuple[str, str, str] | None = None
    for rel in RELATION_PRIORITY:
        for direction in ("out", "in"):
            for pat in RELATION_PATTERNS[rel][direction]:
                if re.search(pat, text):
                    # 同一优先级的 out 优先于 in（"X属于什么" 比 "什么属于X" 常见）
                    if best is None:
                        best = (rel, direction, pat)
                    break
            if best:
                break
        if best:
            break
    return best if best else (None, None, "")


def detect_entity_type(text: str) -> str | None:
    """问句里是否点名了某类实体（"哪些算法…" → 算法）。"""
    for etype, words in TYPE_WORDS:
        for w in words:
            if w in text:
                return etype
    return None


# ---------------------------------------------------------------------------
# 三、查询计划（NL→Cypher 的中间表示）
# ---------------------------------------------------------------------------
@dataclass
class QueryPlan:
    """一次查询的"意图 + 参数"，是规则层与执行层之间的唯一契约。"""
    kind: str                                   # 见文件头 10 类
    question: str
    entities: list[dict] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)   # 允许的关系类型（首个为主）
    direction: str | None = None                # out / in / None（双向）
    entity_type: str | None = None              # 类型过滤（filter / type_list）
    target: str | None = None                   # filter 的目标实体
    keyword: str | None = None                  # search 用
    limit: int = 50
    rule: str = ""                              # 命中的规则，用于解释与答辩演示
    suggestions: list[str] = field(default_factory=list)
    relaxed_from: str | None = None              # 非空表示：原类型查不到，已放开类型限制重查

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entities"] = [{"name": e["name"], "type": e["type"], "id": e["id"]} for e in self.entities]
        return d


def _clean_keyword(text: str) -> str:
    """从问句里剥掉疑问词，留下可能的关键词（兜底检索用）。"""
    kw = re.sub(r"(什么是|是什么|是啥|请问|一下|有哪些|有什么|怎么样|怎样|如何|怎么|为什么|吗|呢|？|\?|的|和|与|及|以及|分别|都|能|可以)", "", text)
    return kw.strip("，,。.、 ")


def build_plan(question: str, limit: int = 50) -> QueryPlan:
    """
    把自然语言问句解析成 QueryPlan。
    规则按优先级排列，先命中先返回 —— 顺序本身就是"歧义消解"策略。
    """
    entities, matches, text = link_entities(question)

    # 两个实体：优先判"对比"，否则按"路径/关联"处理
    if len(entities) >= 2:
        a, b = entities[0], entities[1]
        # 例外：形如 "X 适用于什么 Y" 的问句——第二个实体出现在关系触发词之后，
        # 只是疑问宾语的一部分（如"什么机器学习任务"），语义是查 X 的某类关系，
        # 不是查两个实体之间的路径。此时降级为单实体关系查询。
        rel0, dir0, pat0 = detect_relation(text)
        if rel0 and dir0 == "out":
            m0 = re.search(pat0, text)
            # 位置必须取自实体链接结果：用户可能用的是别名（MSE / 聚类），
            # 用正式名去 text 里 find 会返回 -1，导致判断失效。
            pos: dict[str, int] = {}
            for m in matches:
                pos.setdefault(m.entity["name"], m.start)
            pa = pos.get(a["name"], -1)
            pb = pos.get(b["name"], -1)
            if m0 and pa != -1 and pb != -1 and pa < m0.start() and pb >= m0.end():
                entities = [a]          # 丢弃 b，走下方单实体分支
    if len(entities) >= 2:
        a, b = entities[0], entities[1]
        if re.search(COMPARE_RE, text):
            return QueryPlan("compare", question, [a, b], limit=limit,
                             rule="识别到 2 个实体 + 对比类疑问词 → 共同邻居对比")
        return QueryPlan("path", question, [a, b], limit=limit,
                         rule="识别到 2 个实体 → 最短路径查询")

    rel, direction, pat = detect_relation(text)
    etype = detect_entity_type(text)
    # 列举句式有两种：句中出现"有哪些/包括"，或句首是"哪些/哪个/什么 + 类型词"
    has_list = bool(re.search(LIST_RE, text)) or bool(etype and re.match(LISTING_HEAD_RE, text))

    # 没识别出实体：走类型列表 / 统计 / 关键词兜底
    if not entities:
        # 统计优先："哪个算法关系最多" 虽然也像列举，但问的是排序结果
        if re.search(STATS_RE, text):
            return QueryPlan("stats", question, [], entity_type=etype, limit=limit,
                             rule="统计类问句 → 按度数排序")
        if has_list and etype:
            return QueryPlan("type_list", question, [], entity_type=etype, limit=limit,
                             rule=f"未识别实体，但问句点名了「{etype}」+ 列举句式 → 按类型列表")
        kw = _clean_keyword(text)
        sug = suggest_entities(question)
        return QueryPlan("search", question, [], keyword=kw or text, limit=limit,
                         suggestions=sug,
                         rule="未命中实体与关系触发词 → 关键词模糊检索")

    ent = entities[0]

    # 列举式问句（"哪些算法可以做异常检测"）：实体是宾语，方向要翻转成 in
    if has_list and etype and re.match(LISTING_HEAD_RE, text) and rel in ("属于", "应用于", "衍生自"):
        direction = "in"

    # 单实体 + 关系触发词
    if rel:
        rels = [rel]
        # 评价指标在图里是"属于任务"（P01 属于 分类任务），问"用于"时补上属于
        if ent["type"] == "评价指标" and rel == "应用于":
            rels = ["应用于", "属于"]
        # "哪些指标可以评估分类任务" → 指标类问句本质是 属于 的反向查询
        if etype == "评价指标" and rel == "应用于" and has_list:
            rels, direction = ["属于"], "in"
        if has_list and etype and direction == "in":
            return QueryPlan("filter", question, [ent], relations=rels, direction=direction,
                             entity_type=etype, target=ent["name"], limit=limit,
                             rule=f"「{ent['name']}」+ 列举句式 + 类型「{etype}」→ 类型×关系条件筛选")
        if re.search(STATS_RE, text):
            return QueryPlan("stats", question, [ent], limit=limit,
                             rule="含统计疑问词 → 度数排序")
        return QueryPlan("neighbors", question, [ent], relations=rels, direction=direction,
                         limit=limit,
                         rule=f"命中「{rel}」触发词 /{pat}/ → 一跳关系查询（方向 {direction}）")

    # 单实体 + "什么是X" → 实体详情
    if re.search(DETAIL_RE, text):
        return QueryPlan("detail", question, [ent], limit=limit,
                         rule="命中「什么是/介绍」句式 → 实体详情 + 一跳邻居")

    if re.search(STATS_RE, text):
        return QueryPlan("stats", question, [ent], limit=limit,
                         rule="含统计疑问词 → 按度数排序")

    if has_list and etype:
        return QueryPlan("filter", question, [ent], relations=["属于"], direction="in",
                         entity_type=etype, target=ent["name"], limit=limit,
                         rule=f"「{ent['name']}」+ 类型「{etype}」+ 列举句式 → 条件筛选")

    # 只提到实体，没给关系：默认返回详情（最有用）
    return QueryPlan("detail", question, [ent], limit=limit,
                     rule="仅识别到 1 个实体 → 默认返回详情")


def suggest_entities(question: str, n: int = 3) -> list[str]:
    """问句没命中时，用编辑距离给几个相近实体名，方便用户改问法。"""
    pool = [e["name"] for e in kb.entities]
    hits = get_close_matches(normalize(question), [normalize(p) for p in pool], n=n, cutoff=0.3)
    name_by_norm = {normalize(p): p for p in pool}
    return [name_by_norm[h] for h in hits if h in name_by_norm]


# ---------------------------------------------------------------------------
# 四、模板填充：QueryPlan → 参数化 Cypher
# ---------------------------------------------------------------------------
# 说明：只有"关系类型/实体标签"会被拼进语句，且只能来自本文件的枚举常量；
#      用户输入（实体名、关键词、limit）一律走 $ 参数，杜绝注入。

def _rel_filter(plan: QueryPlan) -> tuple[str, dict]:
    """
    关系过滤统一写成 type(r) IN $rels。
    好处：不管 1 个还是 5 个关系，语句结构和参数永远一致，少一类边界 bug；
    关系类型来自本模块常量，参数值走 $rels，不参与字符串拼接。
    """
    rels = plan.relations or RELATION_TYPES
    return "WHERE type(r) IN $rels", {"rels": rels}


def build_cypher(plan: QueryPlan) -> tuple[str, dict]:
    """对外接口：返回 (cypher, params)，顺手清掉模板里的空行，方便贴进 Neo4j 浏览器演示。"""
    cypher, params = _build_cypher(plan)
    compact = "\n".join(line.rstrip() for line in cypher.strip().splitlines() if line.strip())
    return compact, params


def _build_cypher(plan: QueryPlan) -> tuple[str, dict]:
    """返回 (cypher, params)。params 直接交给 py2neo，不参与字符串拼接。"""
    params: dict = {"limit": max(1, min(plan.limit, 200))}

    if plan.kind in ("neighbors", "detail"):
        name = plan.entities[0]["name"]
        params.update({"name": name, "direction": plan.direction})
        prefix, extra = _rel_filter(plan)
        params.update(extra)
        # 无向匹配 + 方向判定：一条语句同时覆盖 out/in/双向，避免写三份模板
        cypher = f"""
MATCH (n:Entity {{name: $name}})-[r]-(m:Entity)
{prefix}
WITH n, r, m, CASE WHEN startNode(r) = n THEN 'out' ELSE 'in' END AS dir
WHERE ($direction IS NULL OR dir = $direction) AND m.name <> $name
RETURN type(r) AS relation, dir AS direction, m.name AS name, m.type AS type,
       coalesce(r.description, '') AS description,
       coalesce(r.confidence, 0.90) AS confidence
ORDER BY relation, name
LIMIT $limit
"""
        return cypher.strip(), params

    if plan.kind == "filter":
        # entity_type 为空表示"放开类型限制"（原类型查不到时的二次查询）
        label = f":{_label_of(plan.entity_type)}" if plan.entity_type else ""
        params.update({"target": plan.target})
        prefix, extra = _rel_filter(plan)
        params.update(extra)
        # filter 语义固定为 (候选节点)-[关系]->(目标)，目标永远是关系的尾部
        cypher = f"""
MATCH (n{label})-[r]->(t:Entity {{name: $target}})
{prefix}
RETURN n.name AS name, n.type AS type, coalesce(n.description, '') AS description,
       type(r) AS relation, coalesce(r.description, '') AS rel_description,
       coalesce(r.confidence, 0.90) AS confidence, coalesce(r.evidence, '') AS evidence,
       t.name AS target
ORDER BY name
LIMIT $limit
"""
        return cypher.strip(), params

    if plan.kind == "type_list":
        label = _label_of(plan.entity_type)
        cypher = f"""
MATCH (n:{label})
RETURN n.name AS name, n.type AS type, coalesce(n.description, '') AS description
ORDER BY name
LIMIT $limit
"""
        return cypher.strip(), params

    if plan.kind == "compare":
        a, b = plan.entities[0]["name"], plan.entities[1]["name"]
        params.update({"a": a, "b": b})
        cypher = """
MATCH (a:Entity {name: $a})-[r1]-(x:Entity)-[r2]-(b:Entity {name: $b})
WHERE x <> a AND x <> b
WITH x, collect(DISTINCT type(r1))[0] AS rel_a, collect(DISTINCT type(r2))[0] AS rel_b
RETURN x.name AS name, x.type AS type, rel_a, rel_b,
       coalesce(x.description, '') AS description
ORDER BY name
LIMIT $limit
"""
        return cypher.strip(), params

    if plan.kind == "path":
        a, b = plan.entities[0]["name"], plan.entities[1]["name"]
        params.update({"a": a, "b": b})
        params.pop("limit", None)          # 模板里写死 LIMIT 5，不传无用参数
        cypher = """
MATCH (a:Entity {name: $a}), (b:Entity {name: $b})
MATCH p = allShortestPaths((a)-[*..6]-(b))
WITH p LIMIT 5
RETURN length(p) AS hops,
       [n IN nodes(p) | n.name] AS path_names,
       [n IN nodes(p) | n.type] AS path_types,
       [r IN relationships(p) | type(r)] AS path_rels,
       [r IN relationships(p) | coalesce(r.description, '')] AS path_descs,
       [r IN relationships(p) | coalesce(r.confidence, 0.90)] AS path_confs,
       [r IN relationships(p) | startNode(r).name] AS path_srcs,
       [r IN relationships(p) | endNode(r).name] AS path_tgts
"""
        return cypher.strip(), params

    if plan.kind == "stats":
        params.update({"etype": plan.entity_type})
        cypher = """
MATCH (n:Entity)-[r]-()
WHERE $etype IS NULL OR n.type = $etype
RETURN n.name AS name, n.type AS type, count(r) AS degree
ORDER BY degree DESC, name
LIMIT $limit
"""
        return cypher.strip(), params

    # search / not_found
    params.update({"kw": plan.keyword or ""})
    cypher = """
MATCH (n:Entity)
WHERE n.name CONTAINS $kw OR coalesce(n.aliases, '') CONTAINS $kw
   OR coalesce(n.en_name, '') CONTAINS $kw
RETURN n.name AS name, n.type AS type, coalesce(n.description, '') AS description
ORDER BY name
LIMIT $limit
"""
    return cypher.strip(), params


def _label_of(etype: str | None) -> str:
    """中文类型 → Neo4j 标签，白名单校验（拼进 Cypher 前必须过这一关）。"""
    from .kgdata import TYPE_TO_LABEL
    if etype not in TYPE_TO_LABEL:
        raise ValueError(f"非法的实体类型：{etype}")
    return TYPE_TO_LABEL[etype]


# ---------------------------------------------------------------------------
# 五、演示用示例问题（/api/examples 返回给前端做快捷按钮）
# ---------------------------------------------------------------------------
EXAMPLES = [
    {"text": "什么是过拟合？", "intent": "实体详情"},
    {"text": "随机森林属于哪一类算法？", "intent": "一跳关系"},
    {"text": "XGBoost 是从哪个算法改进来的？", "intent": "衍生关系"},
    {"text": "支持向量机的核心原理是什么？", "intent": "基于关系"},
    {"text": "随机森林和梯度提升树有什么区别？", "intent": "双实体对比"},
    {"text": "随机森林和K均值聚类怎么连起来？", "intent": "多跳路径"},
    {"text": "SVM和逻辑回归有什么关系？", "intent": "多跳路径"},          # 答辩演示问句①（别名映射 SVM→支持向量机）
    {"text": "随机森林是基于什么模型衍生的？", "intent": "衍生关系"},      # 答辩演示问句②（触发词歧义消解：衍生 > 基于）
    {"text": "哪些算法可以做异常检测？", "intent": "条件筛选"},
    {"text": "有哪些评价指标？", "intent": "类型列表"},
    {"text": "哪个实体关系最多？", "intent": "统计聚合"},
]
