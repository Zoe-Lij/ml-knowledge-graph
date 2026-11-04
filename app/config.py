#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
config.py
----------
后端统一配置读取。

设计说明：
    1. 所有可变参数都走环境变量 / .env 文件，代码里只留一份"能跑起来的默认值"；
    2. 配置文件在项目根目录（.env），和阶段2的导入脚本共用一份，避免两处密码不一致；
    3. KG_MODE 决定数据来源：
         neo4j —— 只查图数据库（正式演示用）
         local —— 只查本地 kg.json（没装数据库时也能讲清楚系统逻辑）
         auto  —— 优先 Neo4j，连不上自动降级 local（默认，防止答辩现场数据库没起来）
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录：本文件位于 <root>/app/ 下
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# 读取 .env（不存在也不报错，全部走默认值）
load_dotenv(ROOT / ".env")


def _get_bool(key: str, default: bool = False) -> bool:
    """把 "1/true/yes/on" 解析成 True，其余按 False 处理。"""
    return os.getenv(key, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """全局配置对象（模块级单例，import 时创建一次）。"""

    # ---------- 服务 ----------
    APP_NAME: str = "机器学习领域知识图谱系统"
    APP_VERSION: str = "1.0.0"
    HOST: str = os.getenv("API_HOST", "127.0.0.1")
    PORT: int = int(os.getenv("API_PORT", "8000"))

    # ---------- Neo4j 连接 ----------
    # 注意：bolt 协议端口是 7687，不是浏览器界面的 7474
    # 本机：bolt://localhost:7687（py2neo 驱动）
    # 云端：neo4j+s://xxxx.databases.neo4j.io（Aura 托管实例，官方驱动 + TLS）
    NEO4J_URI: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    NEO4J_USER: str = os.getenv("NEO4J_USER", "neo4j")
    NEO4J_PASSWORD: str = os.getenv("NEO4J_PASSWORD", "")  # 不要把真实密码写进代码；从 .env 读取
    NEO4J_DATABASE: str = os.getenv("NEO4J_DATABASE", "neo4j")
    # 驱动选择：空=按 URI 自动判断（bolt:// 用 py2neo，neo4j+s:// 用官方驱动）
    #            official / py2neo = 强制指定（本机验证云端驱动路径时用）
    NEO4J_DRIVER: str = os.getenv("NEO4J_DRIVER", "").strip().lower()

    # ---------- 运行环境 ----------
    # Vercel 会注入 VERCEL=1：Serverless 下连接超时要压短（函数总时长有限）
    IS_SERVERLESS: bool = bool(os.getenv("VERCEL"))

    @property
    def is_serverless(self) -> bool:
        return self.IS_SERVERLESS

    @property
    def neo4j_configured(self) -> bool:
        """
        是否显式配置过数据库连接。

        目的：没有配置时**不要**去试连 localhost:7687。
        Vercel 上默认没有任何 NEO4J_* 变量，若仍按 auto 去试连，
        每个请求都要等一次连接超时（Serverless 冷启动下尤其致命）。
        未配置 → 直接判定为"使用服务端 kg.json 数据集"，接口照常工作。
        """
        return bool(os.getenv("NEO4J_URI") or os.getenv("NEO4J_PASSWORD"))

    # ---------- 数据源模式 ----------
    KG_MODE: str = os.getenv("KG_MODE", "auto").strip().lower()

    # ---------- 查询约束 ----------
    MAX_QUESTION_LEN: int = 200      # 问句最大长度，防止超长文本拖慢正则匹配
    DEFAULT_LIMIT: int = 50          # 单次查询默认返回上限
    MAX_LIMIT: int = 200             # 允许前端调整的上限
    MAX_HOPS: int = 6                # 最短路径最大跳数（图直径 11，6 跳够用且不爆栈）
    GRAPH_CACHE_TTL: int = 60        # /api/graph 结果缓存秒数

    # ---------- 跨域 ----------
    # 阶段4 前端如果直接用 file:// 打开，需要放开跨域；生产部署建议改成具体域名
    CORS_ORIGINS: list = ["*"]

    @property
    def allow_origin_regex(self) -> str:
        return ".*" if "*" in self.CORS_ORIGINS else ""


settings = Settings()
