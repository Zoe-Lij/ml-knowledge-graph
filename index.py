#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
index.py —— Vercel 部署入口（Serverless 入口，不改动 app/ 内任何业务代码）

为什么需要这个文件？
    Vercel 的 Python Runtime 会在仓库根目录寻找 index.py / app.py / main.py 等入口，
    并要求其中有一个名为 `app` 的 ASGI 实例。本项目业务入口是 app/main.py，
    这里再做一层薄封装，只做一件业务代码里不该关心的事：**确保线上连的是云图数据库**。

线上数据源（重要）
    Vercel 是 Serverless，跑不了 Neo4j 进程，所以线上连接的是**托管在 Neo4j Aura 的
    真实图数据库实例**（Neo4j 5.x），通过环境变量注入连接信息：

        NEO4J_URI      = neo4j+s://xxxxxxxx.databases.neo4j.io
        NEO4J_USER     = neo4j
        NEO4J_PASSWORD = <Aura 创建实例时给的密码>
        NEO4J_DATABASE = neo4j
        KG_MODE        = auto        （优先 Neo4j；连不上才降级本地 kg.json 兜底）

    也就是说：线上执行的是与本地**同一套 Cypher**，数据来自云端图库，
    不是把 kg.json 塞进内存冒充数据库。

    降级只作为"数据库临时不可达时页面不至于白屏"的兜底；/api/health 会明确报告
    neo4j.connected 与库内节点/关系数，答辩验收时以这个字段为准。

本地不受影响
    本文件只在 Vercel 上被当成入口；本机启动仍是 app/main.py + .env（bolt://localhost:7687）。
"""

import os

# KG_MODE 默认 auto：线上优先连 Aura，连不上才降级（不覆盖用户显式设置）
os.environ.setdefault("KG_MODE", "auto")

# 云端必须用官方驱动（neo4j+s:// 协议），这里提前给出缺依赖时的可读报错
if os.getenv("VERCEL") and os.getenv("NEO4J_URI", "").startswith(("neo4j+s://", "neo4j://")):
    try:
        import neo4j  # noqa: F401
    except ImportError:  # pragma: no cover - 构建期就该暴露，而不是运行期 500
        raise RuntimeError(
            "线上连接 Aura 需要官方驱动：requirements.txt 里应有 neo4j>=5.14"
        )

from app.main import app  # noqa: E402  (必须放在环境变量设置之后)

__all__ = ["app"]
