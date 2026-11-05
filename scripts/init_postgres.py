#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
init_postgres.py
-----------------
把 data/kg.json 初始化到 PostgreSQL（线上托管库：Neon / Supabase / 云厂商 RDS 都行）。

用法：
    # 1) 建表 + 灌数据（可重复执行，重复跑就是"覆盖更新"，不会产生重复行）
    export DATABASE_URL='postgresql://user:pass@ep-xxx.aws.neon.tech/neondb?sslmode=require'
    python scripts/init_postgres.py

    # 2) 清空重建（改了 schema 或数据大改时用）
    python scripts/init_postgres.py --reset

    # 3) 灌完立刻自检：跑 11 条示例问句，确认 NL→SQL→数据库 全链路通
    python scripts/init_postgres.py --verify

    # 连接串也可以直接传参（优先级高于环境变量）
    python scripts/init_postgres.py --url 'postgresql://...'

设计说明（答辩可能会问）：
    · 为什么用 PostgreSQL 而不是把 Neo4j 搬上云？
      Vercel 的 Serverless 函数不能跑常驻数据库进程，Neo4j Aura 免费档在中国区
      注册受限；托管 PostgreSQL 有免费额度、给的就是标准连接串，换上就能用。
    · 图结构怎么落到关系库？
      节点一张表（kg_entity）、边一张表（kg_relation），边上带 rel_type 和方向；
      多跳路径用 WITH RECURSIVE 递归 CTE 在库里做 BFS，不把数据拉到应用层。
    · 数据会不会丢？
      数据在云上，Vercel 每次冷启动都从库里读一遍，重新部署/换电脑/刷新页面都不影响。
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DDL = """
CREATE TABLE IF NOT EXISTS kg_entity (
    id          text PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    type        text NOT NULL,
    label       text NOT NULL DEFAULT '',
    en_name     text NOT NULL DEFAULT '',
    aliases     text NOT NULL DEFAULT '',
    description text NOT NULL DEFAULT '',
    color       text NOT NULL DEFAULT '',
    confidence  double precision NOT NULL DEFAULT 1.0,
    source      text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_kg_entity_type ON kg_entity (type);

CREATE TABLE IF NOT EXISTS kg_relation (
    id          bigserial PRIMARY KEY,
    rid         text NOT NULL DEFAULT '',
    source_name text NOT NULL REFERENCES kg_entity (name) ON UPDATE CASCADE ON DELETE CASCADE,
    target_name text NOT NULL REFERENCES kg_entity (name) ON UPDATE CASCADE ON DELETE CASCADE,
    rel_type    text NOT NULL,
    description text NOT NULL DEFAULT '',
    is_symmetric boolean NOT NULL DEFAULT false,
    confidence  double precision NOT NULL DEFAULT 0.90,
    evidence    text NOT NULL DEFAULT '',
    UNIQUE (source_name, target_name, rel_type)
);
CREATE INDEX IF NOT EXISTS idx_kg_relation_source ON kg_relation (source_name);
CREATE INDEX IF NOT EXISTS idx_kg_relation_target ON kg_relation (target_name);
CREATE INDEX IF NOT EXISTS idx_kg_relation_type   ON kg_relation (rel_type);

CREATE TABLE IF NOT EXISTS kg_meta (
    key   text PRIMARY KEY,
    value jsonb NOT NULL
);
"""

DROP = """
DROP TABLE IF EXISTS kg_relation;
DROP TABLE IF EXISTS kg_entity;
DROP TABLE IF EXISTS kg_meta;
"""


def _dsn(arg_url: str | None) -> str:
    url = (arg_url or os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or "").strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if not url:
        sys.exit(
            "没有拿到连接串。请先设置环境变量：\n"
            "  export DATABASE_URL='postgresql://user:pass@ep-xxx.aws.neon.tech/neondb?sslmode=require'\n"
            "或用参数传入：python scripts/init_postgres.py --url 'postgresql://...'"
        )
    return url


def _mask(url: str) -> str:
    """打印连接串时把密码打码，避免截图/日志泄露。"""
    import re
    return re.sub(r"(://[^:]+:)[^@]+@", r"\1****@", url)


def connect(dsn: str):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(
        dsn,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=10,
        # Neon 的 pooled 连接串走 PgBouncer，必须关掉服务端预处理语句
        # （见 docs/06_vercel_deploy.md 的踩坑记录）
        prepare_threshold=None,
        application_name="ml-kg-init",
    )


def load_seed(raw: dict) -> tuple[list[dict], list[dict]]:
    """kg.json → 两张表的行（关系表用实体名做外键，查询时省一次 JOIN）。"""
    name_of = {e["id"]: e["name"] for e in raw["entities"]}
    entities = [
        {
            "id": e["id"],
            "name": e["name"],
            "type": e["type"],
            "label": e.get("label", ""),
            "en_name": e.get("en_name", ""),
            "aliases": e.get("aliases", ""),
            "description": e.get("description", ""),
            "color": e.get("color", ""),
            "confidence": float(e.get("confidence", 1.0) or 1.0),
            "source": e.get("source", ""),
        }
        for e in raw["entities"]
    ]
    relations = [
        {
            "rid": r.get("id", ""),
            "source_name": name_of.get(r["source"], r["source"]),
            "target_name": name_of.get(r["target"], r["target"]),
            "rel_type": r["type"],
            "description": r.get("description", ""),
            "symmetric": bool(r.get("symmetric", False)),
            "confidence": float(r.get("confidence", 0.90) or 0.90),
            "evidence": r.get("evidence", ""),
        }
        for r in raw["relations"]
    ]
    return entities, relations


def create_schema(conn, reset: bool = False) -> None:
    if reset:
        conn.execute(DROP)
        print("· 已删除旧表（--reset）")
    conn.execute(DDL)
    print("· 表结构就绪：kg_entity / kg_relation / kg_meta")


def seed(conn, raw: dict, reset: bool = False) -> tuple[int, int]:
    entities, relations = load_seed(raw)

    # 先清边再灌边：外键约束要求节点先存在；--reset 时表本来已空
    conn.execute("DELETE FROM kg_relation")
    conn.execute("DELETE FROM kg_entity")

    conn.cursor().executemany(
        """
INSERT INTO kg_entity (id, name, type, label, en_name, aliases, description, color, confidence, source)
VALUES (%(id)s, %(name)s, %(type)s, %(label)s, %(en_name)s, %(aliases)s,
        %(description)s, %(color)s, %(confidence)s, %(source)s)
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name, type = EXCLUDED.type, label = EXCLUDED.label,
    en_name = EXCLUDED.en_name, aliases = EXCLUDED.aliases,
    description = EXCLUDED.description, color = EXCLUDED.color,
    confidence = EXCLUDED.confidence, source = EXCLUDED.source
""",
        entities,
    )
    conn.cursor().executemany(
        """
INSERT INTO kg_relation (rid, source_name, target_name, rel_type, description,
                         is_symmetric, confidence, evidence)
VALUES (%(rid)s, %(source_name)s, %(target_name)s, %(rel_type)s, %(description)s,
        %(symmetric)s, %(confidence)s, %(evidence)s)
ON CONFLICT (source_name, target_name, rel_type) DO UPDATE SET
    rid = EXCLUDED.rid, description = EXCLUDED.description,
    is_symmetric = EXCLUDED.is_symmetric, confidence = EXCLUDED.confidence,
    evidence = EXCLUDED.evidence
""",
        relations,
    )

    # 元信息（版本号、置信度模型）整份存成 jsonb，前端图例直接读
    meta = raw.get("meta", {})
    conn.cursor().executemany(
        """
INSERT INTO kg_meta (key, value) VALUES (%(k)s, %(v)s::jsonb)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
""",
        [{"k": k, "v": json.dumps(v, ensure_ascii=False)} for k, v in meta.items()],
    )
    return len(entities), len(relations)


def stats(conn) -> dict:
    ent = conn.execute("SELECT count(*)::int AS c FROM kg_entity").fetchone()["c"]
    rel = conn.execute("SELECT count(*)::int AS c FROM kg_relation").fetchone()["c"]
    by_type = conn.execute(
        "SELECT type, count(*)::int AS c FROM kg_entity GROUP BY type ORDER BY c DESC"
    ).fetchall()
    by_rel = conn.execute(
        "SELECT rel_type, count(*)::int AS c FROM kg_relation GROUP BY rel_type ORDER BY c DESC"
    ).fetchall()
    return {"entities": ent, "relations": rel, "by_type": by_type, "by_relation": by_rel}


def verify(dsn: str) -> int:
    """
    全链路自检：11 条示例问句 → 规则解析 → 生成 SQL → 打到 PostgreSQL → 出答案。
    这是"线上真的连着数据库"最直接的证据（答辩时可现场跑）。
    """
    os.environ["DATABASE_URL"] = dsn
    os.environ.setdefault("KG_MODE", "postgres")
    from app.nl2cypher import EXAMPLES, build_plan
    from app.store import postgres_store

    if not postgres_store.connect(force=True):
        print("✗ 连接失败：", postgres_store.last_error)
        return 1

    ok = 0
    print("\n全链路自检（问句 → SQL → PostgreSQL）：")
    for ex in EXAMPLES:
        q = ex["text"]
        try:
            plan = build_plan(q, limit=50)
            result = postgres_store.query(plan)
            n = len(result.get("rows") or [])
            extra = ""
            if result.get("path"):
                extra = f"，路径 {' → '.join(result['path']['names'])}"
            elif result.get("direct"):
                extra = f"，直接关系 {len(result['direct'])} 条"
            elif plan.kind == "path" and not result.get("path"):
                extra = "，未找到路径"
            print(f"  ✓ {q}  [{plan.kind}] rows={n}{extra}")
            ok += 1
        except Exception as exc:
            print(f"  ✗ {q}  [{ex['intent']}] {type(exc).__name__}: {exc}")
    print(f"\n{ok}/{len(EXAMPLES)} 条问句通过")
    return 0 if ok == len(EXAMPLES) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="初始化 PostgreSQL 知识图谱库")
    ap.add_argument("--url", help="数据库连接串（默认读 DATABASE_URL）")
    ap.add_argument("--reset", action="store_true", help="先删表再重建")
    ap.add_argument("--verify", action="store_true", help="灌完立刻跑 11 条问句自检")
    args = ap.parse_args()

    dsn = _dsn(args.url)
    print(f"· 目标数据库：{_mask(dsn)}")

    raw = json.loads((ROOT / "data" / "kg.json").read_text(encoding="utf-8"))

    with connect(dsn) as conn:
        create_schema(conn, reset=args.reset)
        n_ent, n_rel = seed(conn, raw, reset=args.reset)
        print(f"· 已写入 {n_ent} 个实体、{n_rel} 条关系")
        s = stats(conn)
        print(f"· 库内实际：实体 {s['entities']}，关系 {s['relations']}")
        print("  按类型：" + "、".join(f"{r['type']} {r['c']}" for r in s["by_type"]))
        print("  按关系：" + "、".join(f"{r['rel_type']} {r['c']}" for r in s["by_relation"]))

        if (s["entities"], s["relations"]) != (n_ent, n_rel):
            print("✗ 数量对不上，请检查写入是否被约束拦截")
            return 1
        print("✓ 初始化完成")

    if args.verify:
        return verify(dsn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
