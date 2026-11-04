#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
security.py
------------
Cypher 安全护栏（产品优化点 1）。

为什么要单独一个模块？
    "把用户输入拼进 Cypher"是图数据库项目最常见的安全漏洞来源：
        question = "x' OR 1=1 MATCH (n) DETACH DELETE n //"
    一旦拼进语句，轻则查错，重则删库。本项目的做法是**四层防御**，
    并且把每一层都写成可单独测试的函数，答辩时能当场演示拦截效果。

四层防御（由外到内）：
    L1 输入筛查  screen_question()  —— 问句里出现 Cypher 关键字/语法符号，直接拒绝；
    L2 语句校验  enforce()          —— 只允许单条只读语句，白名单开头 + 黑名单关键字；
    L3 标识符白名单                 —— 标签只能是 4 类实体标签，关系类型只能是 5 种；
    L4 参数化执行                   —— 值一律走 $param，绝不参与字符串拼接。

补充说明（也是设计取舍）：
    - 本项目**没有**实现完整的 Cypher 语法解析器。课程项目里 8 个模板是固定的，
      真正的安全来自"用户输入永远不进入语句文本"，正则校验是兜底；
    - 因此校验采用"**白名单为主、黑名单为辅**"：开头必须是 MATCH/WITH/UNWIND/RETURN，
      出现 CREATE/MERGE/DELETE 等写关键字即拒绝，宁可误杀不可放过；
    - 生产环境还应给接口配只读数据库账号（见 docs/04_security_and_confidence.md 第 5 节）。
"""

import json
import os
import re
import threading
import time
from collections import Counter

from .config import ROOT
from .kgdata import RELATION_TYPES, TYPE_TO_LABEL

# 允许出现在 Cypher 里的节点标签（:Entity + 4 类业务标签）
ALLOWED_LABELS = set(TYPE_TO_LABEL.values()) | {"Entity"}

# 允许出现的关系类型（中文，来自 Schema 白名单）
ALLOWED_RELATIONS = set(RELATION_TYPES)

# 写操作 / 管理操作关键字：出现即判定为非法（读接口不该有任何写语句）
FORBIDDEN_KEYWORDS = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|CALL|LOAD|CSV|"
    r"GRANT|REVOKE|INDEX|CONSTRAINT|UNION|PERIODIC|COMMIT|ROLLBACK|CYPHER)\b",
    re.IGNORECASE,
)

# 允许出现在语句结尾/中间的只读子句（仅用于第一个关键字的检查）
ALLOWED_START = ("MATCH", "OPTIONAL MATCH", "WITH", "UNWIND", "RETURN")

# 危险的过程/函数前缀（APOC、图算法库、系统库都能改数据）
FORBIDDEN_PREFIX = re.compile(r"\b(apoc|gds|db|dbms)\.", re.IGNORECASE)

# 问句层面：这些片段在中文自然语言问句里几乎不可能出现，出现就高度可疑
_SUSPICIOUS_PATTERNS = [
    (re.compile(r"[;`{}\\]"), "包含 Cypher 语法符号（; ` { } \\）"),
    (re.compile(r"\$\{|\$[A-Za-z_]"), "包含参数注入片段（$xxx / ${...}）"),
    (re.compile(r"(--|/\*)"), "包含注释片段（-- 或 /*）"),
    (re.compile(r"\b(MATCH|MERGE|DETACH|UNWIND|CYPHER|APOC|LOAD\s+CSV|FOREACH)\b", re.I),
     "包含 Cypher 关键字"),
    (re.compile(r"\b(DELETE|CREATE|DROP|SET|CALL|RETURN|UNION|REMOVE)\b"), "包含 Cypher 关键字"),
    (re.compile(r"\b(OR|AND)\b\s+1\s*=\s*1", re.I), "包含恒真注入片段（1=1）"),
]

# 参数值限制（防止用超长字符串做拒绝服务）
MAX_CYPHER_LEN = 4000
MAX_PARAM_COUNT = 12
MAX_PARAM_STR = 200
MAX_PARAM_LIST = 50


class SecurityError(Exception):
    """安全校验未通过。reason 面向开发者，message 面向用户。"""

    def __init__(self, message: str, reason: str = "", stage: str = "cypher"):
        super().__init__(message)
        self.message = message
        self.reason = reason or message
        self.stage = stage


# ---------------------------------------------------------------------------
# 一、工具函数
# ---------------------------------------------------------------------------
def strip_literals(cypher: str) -> tuple[str, bool]:
    """
    把字符串字面量替换成 ''，只留下"代码部分"再做关键字扫描。

    两个目的：
        1. 避免误杀：语句里合法的空串 ''、描述文本不会触发关键字检查；
        2. 防止绕过：攻击者把关键字藏进字符串里也没用，因为真正的结构关键字
           必须在字符串之外才会被执行。
    返回 (代码部分, 是否有未闭合的字面量)。
    """
    out, quote, escaped, closed = [], None, False, True
    for ch in cypher:
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
                out.append("''")        # 用一个空字面量占位，保持语句结构
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        out.append(ch)
    if quote:
        closed = False
    return "".join(out), closed


def _check_params(params: dict | None) -> None:
    """参数只允许基本类型，长度受限。参数值本身不会被 Cypher 解析，但仍做防御。"""
    if not params:
        return
    if not isinstance(params, dict):
        raise SecurityError("查询参数格式非法（必须是字典）", "params 不是 dict")
    if len(params) > MAX_PARAM_COUNT:
        raise SecurityError("查询参数过多，已拒绝执行", "参数个数超过 %d" % MAX_PARAM_COUNT)
    for key, val in params.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise SecurityError("查询参数名非法：%r" % (key,), "参数名非法")
        values = val if isinstance(val, list) else [val]
        if isinstance(val, list) and len(val) > MAX_PARAM_LIST:
            raise SecurityError("查询参数 %s 的列表过长" % key, "参数列表过长")
        for v in values:
            # None 是合法参数值：模板里用 `$direction IS NULL` 表示"不限方向"
            if v is None:
                continue
            if isinstance(v, bool) or isinstance(v, (int, float)):
                continue
            if not isinstance(v, str):
                raise SecurityError("查询参数 %s 的类型不允许：%s" % (key, type(v).__name__),
                                    "参数类型非法")
            if len(v) > MAX_PARAM_STR:
                raise SecurityError("查询参数 %s 的内容过长" % key, "参数内容过长")
            if any(ord(c) < 32 for c in v):
                raise SecurityError("查询参数 %s 含控制字符" % key, "参数含控制字符")


# ---------------------------------------------------------------------------
# 二、护栏主体
# ---------------------------------------------------------------------------
class CypherGuard:
    """Cypher 只读校验器 + 拦截审计（线程安全，供多 worker 共用）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.checked = 0            # 校验过的语句数
        self.blocked = 0            # 被拦截数
        self.reasons = Counter()    # 拦截原因分布
        self.recent = []            # 最近 10 条拦截样本（答辩演示用）
        self._log_path = os.path.join(str(ROOT), "logs", "security.log")

    # -- 审计 ----------------------------------------------------------
    def _audit(self, stage: str, reason: str, payload: str) -> None:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self.blocked += 1
            self.reasons[reason] += 1
            self.recent.append({"time": now, "stage": stage, "reason": reason,
                                "payload": payload[:120]})
            self.recent = self.recent[-10:]
        # 审计日志：失败不影响主流程（比如目录不可写时不该让接口 500）
        try:
            os.makedirs(os.path.dirname(self._log_path), exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"time": now, "stage": stage, "reason": reason,
                                    "payload": payload[:300]}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # -- L1：问句筛查 ---------------------------------------------------
    def screen_question(self, question: str) -> None:
        """在翻译之前先看问句。真正的防线在 L2/L4，这一层负责"劝退 + 留痕"。"""
        with self._lock:
            self.checked += 1
        for pattern, reason in _SUSPICIOUS_PATTERNS:
            if pattern.search(question):
                self._audit("question", reason, question)
                raise SecurityError(
                    "问句里出现了 Cypher 语句片段，本接口只接受自然语言问题。"
                    "请用「随机森林属于哪一类算法」这样的问法。",
                    reason, stage="question")

    # -- L2/L3：语句校验 ------------------------------------------------
    def enforce(self, cypher: str, params: dict | None = None) -> None:
        """
        校验一条即将执行的 Cypher。不通过就抛 SecurityError，通过则静默返回。
        每次允许执行前都会调用：模板生成之后一次（main.py）、真正下发给数据库前再一次（store.py）。
        """
        with self._lock:
            self.checked += 1

        if not isinstance(cypher, str) or not cypher.strip():
            raise SecurityError("空语句，已拒绝执行", "空语句")
        if len(cypher) > MAX_CYPHER_LEN:
            raise SecurityError("语句过长，已拒绝执行", "语句超过 %d 字符" % MAX_CYPHER_LEN)

        code, closed = strip_literals(cypher)
        if not closed:
            self._audit("cypher", "字符串字面量未闭合", cypher)
            raise SecurityError("语句中存在未闭合的引号，已拒绝执行", "字面量未闭合")

        # 1) 单语句：分号 = 多语句拼接的标准手法
        if ";" in code:
            self._audit("cypher", "包含分号（多语句）", cypher)
            raise SecurityError("只允许执行单条语句，已拒绝执行", "多语句")

        # 2) 注释：// 和 /* 常被用来截断后半句
        if "//" in code or "/*" in code:
            self._audit("cypher", "包含注释片段", cypher)
            raise SecurityError("语句中不允许出现注释，已拒绝执行", "注释片段")

        # 3) 反引号：可绕过标签白名单（`Entity`) DETACH DELETE ...）
        if "`" in code:
            self._audit("cypher", "包含反引号标识符", cypher)
            raise SecurityError("语句中不允许出现反引号，已拒绝执行", "反引号标识符")

        # 4) 必须以只读子句开头
        head = code.strip().lstrip("(").upper()
        if not any(head.startswith(k) for k in ALLOWED_START):
            self._audit("cypher", "非只读语句开头", cypher)
            raise SecurityError("只允许执行 MATCH / WITH / UNWIND / RETURN 开头的只读查询",
                                "非只读开头")

        # 5) 写操作关键字黑名单
        hit = FORBIDDEN_KEYWORDS.search(code)
        if hit:
            self._audit("cypher", "命中写操作关键字：%s" % hit.group(0).upper(), cypher)
            raise SecurityError("检测到写操作关键字 %s，已拒绝执行" % hit.group(0).upper(),
                                "写操作关键字")

        # 6) 危险过程库（APOC / GDS / db.* 都能间接改数据）
        hit = FORBIDDEN_PREFIX.search(code)
        if hit:
            self._audit("cypher", "调用受限过程库：%s" % hit.group(0), cypher)
            raise SecurityError("不允许调用 %s 开头的系统过程" % hit.group(0), "受限过程库")

        # 7) 必须返回结果（纯 SET 之类即使漏网也不会返回，双重保险）
        if "RETURN" not in code.upper():
            self._audit("cypher", "缺少 RETURN", cypher)
            raise SecurityError("只读查询必须包含 RETURN 子句", "缺少 RETURN")

        # 8) 标签白名单：`:Xxx` 里的 Xxx 只能是 Entity 或 4 类业务标签
        for label in re.findall(r":\s*([A-Za-z_][A-Za-z0-9_]*)", code):
            if label not in ALLOWED_LABELS:
                self._audit("cypher", "非法标签：%s" % label, cypher)
                raise SecurityError("查询使用了未授权的节点标签：%s" % label, "非法标签")

        # 9) 关系类型白名单：模板里出现的 `:类型` 若含中文，必须属于 5 种关系
        for rel in re.findall(r":\s*([\u4e00-\u9fa5]+)", code):
            if rel not in ALLOWED_RELATIONS:
                self._audit("cypher", "非法关系类型：%s" % rel, cypher)
                raise SecurityError("查询使用了未授权的关系类型：%s" % rel, "非法关系类型")

        # 10) 参数检查
        try:
            _check_params(params)
        except SecurityError as exc:
            self._audit("params", exc.reason, json.dumps(params, ensure_ascii=False))
            raise

    # -- 报告 ----------------------------------------------------------
    def report(self) -> dict:
        """给运维 / 答辩演示用的拦截报告。"""
        with self._lock:
            return {
                "checked": self.checked,
                "blocked": self.blocked,
                "block_rate": round(self.blocked / self.checked, 4) if self.checked else 0.0,
                "by_reason": dict(self.reasons),
                "recent": list(self.recent),
                "policy": {
                    "allowed_start": list(ALLOWED_START),
                    "allowed_labels": sorted(ALLOWED_LABELS),
                    "allowed_relations": sorted(ALLOWED_RELATIONS),
                    "forbidden_keywords": FORBIDDEN_KEYWORDS.pattern,
                    "max_cypher_len": MAX_CYPHER_LEN,
                    "param_bound": True,
                },
            }


# 模块级单例：与 kb 一样，全进程共享一份计数
guard = CypherGuard()
