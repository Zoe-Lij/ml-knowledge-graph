#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
index.py —— Vercel 部署入口（Serverless 入口，不改动 app/ 内任何业务代码）

为什么需要这个文件？
    Vercel 的 Python Runtime 会在仓库根目录寻找 index.py，并要求其中有一个名为 `app`
    的 ASGI 实例；本项目的业务入口是 app/main.py，这里只做一层薄封装。

线上数据源（重要）
    Vercel 是 Serverless，函数进程随时回收，**跑不了常驻数据库进程**；
    Neo4j Aura 免费档在中国区注册受限，所以线上主数据源改成了**托管 PostgreSQL**
    （Neon / Supabase / 云厂商 RDS 通用），连接信息通过环境变量注入：

        DATABASE_URL = postgresql://user:pass@ep-xxx-pooler.xxx.aws.neon.tech/neondb?sslmode=require
        KG_MODE      = auto        （neo4j 没配时会自动用 postgres，连不上才降级本地数据）

    表结构：kg_entity（节点）/ kg_relation（边）/ kg_meta（元信息），
    查询全部是参数化 SQL，多跳路径用递归 CTE 在数据库内做 BFS。
    初始化：python scripts/init_postgres.py --verify（详见 docs/06_vercel_deploy.md）

降级策略
    数据库临时不可达时，服务端仍会用随包分发的 data/kg.json 兜底，页面不至于白屏；
    /api/health 会如实报告 store（postgres / neo4j / kg.json）与库内节点、关系数，
    验收时以这个字段为准。

本地不受影响
    本文件只在 Vercel 上被当成入口；本机启动仍是 app/main.py + .env。
"""

import os

# KG_MODE 默认 auto：线上自动选可用的数据源（不覆盖用户显式设置）
os.environ.setdefault("KG_MODE", "auto")

# 云端 Neo4j 必须用官方驱动（neo4j+s:// 协议），缺依赖时给出可读报错而不是运行期 500
if os.getenv("VERCEL") and os.getenv("NEO4J_URI", "").startswith(("neo4j+s://", "neo4j://")):
    try:
        import neo4j  # noqa: F401
    except ImportError:  # pragma: no cover
        raise RuntimeError(
            "线上连接 Aura 需要官方驱动：requirements.txt 里应有 neo4j>=5.14"
        )

# 配了 DATABASE_URL 就必须有 psycopg，否则启动即报错（比运行期 503 更容易定位）
if os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL"):
    try:
        import psycopg  # noqa: F401
    except ImportError:  # pragma: no cover
        raise RuntimeError(
            "线上使用 PostgreSQL 需要驱动：requirements.txt 里应有 psycopg[binary]>=3.1"
        )

from app.main import app  # noqa: E402  (必须放在环境变量设置之后)

__all__ = ["app"]
