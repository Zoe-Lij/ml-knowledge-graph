#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
deploy_vercel.py —— 用 Vercel 官方 REST API 部署（不需要安装 Vercel CLI）

线上架构（重点：连的是真图数据库）
    浏览器 → Vercel Serverless（FastAPI）→ Bolt over TLS → Neo4j Aura（云端 5.x 实例）
    后端不是"读本地 JSON"：/api/query 的每条 Cypher 都由云端图库执行。

部署前必须准备的环境变量（脚本会写入 Vercel 项目设置，无需在网页上手工填）：
    NEO4J_URI       neo4j+s://xxxxxxxx.databases.neo4j.io
    NEO4J_USER      neo4j
    NEO4J_PASSWORD  Aura 实例密码
    NEO4J_DATABASE  neo4j
    KG_MODE         auto      （优先连图库；图库不可达才降级兜底）

用法：
    export VERCEL_TOKEN=xxxx
    export NEO4J_URI=neo4j+s://...  NEO4J_PASSWORD=...
    .venv/bin/python scripts/deploy_vercel.py              # 建项目 + 写环境变量 + 部署 + 自检
    .venv/bin/python scripts/deploy_vercel.py --verify URL  # 只对已有地址验收

自检 6 项（模拟"换台电脑打开"）：
    1. GET /                    → 200 且返回 HTML
    2. GET /api/health          → neo4j.connected=true，库内 113 节点 / 258 关系
    3. GET /api/graph           → 113 节点 / 258 边
    4. GET /api/query 单跳问答  → 200 且返回中文解释
    5. GET /api/query 多跳路径  → 200 且返回 hops ≥ 1 的链路
    6. POST /api/query 注入     → 4xx（安全护栏线上生效）
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.vercel.com"

# 需要上传的路径（相对项目根目录）
INCLUDE_FILES = ["index.py", "vercel.json", "requirements.txt", "requirements-vercel.txt"]
INCLUDE_DIRS = ["app", "data", "web"]
SKIP_NAMES = {".DS_Store", ".env"}
SKIP_DIRS = {"__pycache__", ".git", ".venv"}

PROJECT_NAME = "ml-knowledge-graph"

# 线上环境变量：从本机 shell 读出后写进 Vercel 项目
ENV_KEYS = ["NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "NEO4J_DATABASE", "KG_MODE"]
ENV_DEFAULTS = {"NEO4J_USER": "neo4j", "NEO4J_DATABASE": "neo4j", "KG_MODE": "auto"}


def token() -> str:
    t = os.environ.get("VERCEL_TOKEN", "").strip()
    if not t:
        sys.exit("缺少环境变量 VERCEL_TOKEN（在 https://vercel.com/account/tokens 创建）")
    return t


def api(method: str, path: str, body: dict | None = None,
        timeout: int = 120) -> tuple[int, dict]:
    """调 Vercel API，返回 (状态码, 解析后的 JSON)；4xx/5xx 不退出，交给调用方判断。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token())
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def load_local_env() -> None:
    """
    从项目根的 .env.vercel 读取凭据（存在才读，且不覆盖已存在的 shell 环境变量）。
    这样密码可以只写在本地文件里，不必贴进聊天记录或提交到仓库。
    """
    p = ROOT / ".env.vercel"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v
    print(f"[ENV] 已读取本地凭据文件 {p.name}")


def require_env() -> dict:
    """检查部署所需的环境变量（数据库连接信息缺一不可）。"""
    missing = [k for k in ("NEO4J_URI", "NEO4J_PASSWORD") if not os.environ.get(k)]
    if missing:
        sys.exit("缺少环境变量：" + "、".join(missing)
                 + "\n  示例：export NEO4J_URI='neo4j+s://xxxx.databases.neo4j.io' NEO4J_PASSWORD='...'")
    env = {k: os.environ.get(k, ENV_DEFAULTS.get(k, "")) for k in ENV_KEYS}
    if not env["NEO4J_URI"].startswith(("neo4j+s://", "neo4j://", "bolt+s://")):
        print("[WARN] NEO4J_URI 不是 neo4j+s:// 开头，云端实例通常必须带 +s（TLS）")
    return env


def ensure_project() -> None:
    code, data = api("POST", "/v10/projects", {"name": PROJECT_NAME, "framework": None})
    if code in (200, 201):
        print(f"[PROJECT] 已创建：{PROJECT_NAME}")
    elif code in (400, 409):
        print(f"[PROJECT] 已存在，复用：{PROJECT_NAME}")
    else:
        sys.exit(f"[FAIL] 创建项目失败 HTTP {code}: {json.dumps(data, ensure_ascii=False)[:300]}")


def push_env(env: dict) -> None:
    for k, v in env.items():
        code, data = api("POST", f"/v10/projects/{PROJECT_NAME}/env?upsert=true",
                         {"key": k, "value": v, "type": "encrypted",
                          "target": ["production", "preview", "development"]})
        if code not in (200, 201):
            sys.exit(f"[FAIL] 写入环境变量 {k} 失败 HTTP {code}: "
                     f"{json.dumps(data, ensure_ascii=False)[:300]}")
        shown = v if k != "NEO4J_PASSWORD" else "*" * 12
        print(f"      [ENV] {k} = {shown}")


def collect_files() -> list[dict]:
    """收集要上传的文件，返回 Vercel API 要求的 files 数组。"""
    files: list[dict] = []
    for name in INCLUDE_FILES:
        p = ROOT / name
        if p.exists():
            files.append({"file": name, "data": p.read_text(encoding="utf-8"), "encoding": "utf-8"})
    for d in INCLUDE_DIRS:
        base = ROOT / d
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            if p.name in SKIP_NAMES or any(part in SKIP_DIRS for part in p.parts):
                continue
            rel = p.relative_to(ROOT).as_posix()
            try:
                content = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                print(f"  [SKIP] 非文本文件：{rel}")
                continue
            files.append({"file": rel, "data": content, "encoding": "utf-8"})
    return files


def deploy() -> str:
    files = collect_files()
    total = sum(len(f["data"]) for f in files)
    print(f"[1/4] 收集文件 {len(files)} 个，合计 {total/1024:.0f} KB")
    for f in files:
        print(f"      {f['file']}")

    body = {
        "name": PROJECT_NAME,
        "files": files,
        "target": "production",
        "projectSettings": {
            "framework": None,                      # 不套前端框架预设，交给 Python 运行时自动识别
            "installCommand": "pip install -r requirements-vercel.txt",
        },
    }
    print("[2/4] 提交部署 …")
    code, dep = api("POST", "/v13/deployments?forceNew=1", body)
    if code not in (200, 201):
        sys.exit(f"[FAIL] 提交部署失败 HTTP {code}: {json.dumps(dep, ensure_ascii=False)[:500]}")
    dep_id, url = dep["id"], dep["url"]
    print(f"      deployment id = {dep_id}\n      预览地址 = https://{url}")

    print("[3/4] 等待构建完成（首次约 1–3 分钟）…")
    for i in range(90):
        time.sleep(4)
        _c, info = api("GET", f"/v13/deployments/{dep_id}")
        state = info.get("readyState") or info.get("status")
        print(f"      [{i*4:>3}s] {state}")
        if state == "READY":
            return "https://" + (info.get("url") or url)
        if state in {"ERROR", "CANCELED"}:
            sys.exit(f"[FAIL] 构建失败：{json.dumps(info, ensure_ascii=False)[:800]}")
    sys.exit("[FAIL] 等待超时，请到 Vercel Dashboard 查看构建日志")


def get(url: str, timeout: int = 60) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "deploy-check/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def post_json(url: str, payload: dict) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def verify(base: str) -> int:
    """线上自检：模拟另一台电脑直接访问。返回失败项数。"""
    print(f"\n===== 线上自检 {base} =====")
    fails = 0

    def check(ok: bool, label: str, detail: str = "") -> None:
        nonlocal fails
        print(f"[{'PASS' if ok else 'FAIL'}] {label:<14}{detail}")
        fails += 0 if ok else 1

    code, body = get(base + "/")
    check(code == 200 and b"<html" in body.lower(), "首页", f"HTTP {code}, {len(body)} 字节")

    code, body = get(base + "/api/health")
    data = json.loads(body or b"{}")
    neo = data.get("neo4j") or {}
    connected = neo.get("connected") is True
    ok = code == 200 and connected and neo.get("nodes") == 113 and neo.get("relationships") == 258
    check(ok, "图数据库连接",
          f"HTTP {code}, kg_mode={data.get('kg_mode')}, connected={connected}, "
          f"{neo.get('nodes')} 节点 / {neo.get('relationships')} 关系")
    if not connected:
        print(f"       └─ 连接错误：{neo.get('error')}")

    code, body = get(base + "/api/graph?refresh=true")
    data = json.loads(body or b"{}")
    nodes, links = len(data.get("nodes", [])), len(data.get("links", []))
    check(code == 200 and nodes == 113 and links == 258, "整图数据",
          f"HTTP {code}, {nodes} 节点 / {links} 边")

    q = urllib.parse.quote("SVM和逻辑回归有什么关系？")
    code, body = get(f"{base}/api/query?q={q}")
    data = json.loads(body or b"{}")
    ok = code == 200 and bool(data.get("answer") or data.get("rows"))
    check(ok, "单跳问答",
          f"HTTP {code}, intent={data.get('intent')}, {str(data.get('answer'))[:40]}")

    q = urllib.parse.quote("随机森林和K均值聚类怎么连起来？")
    code, body = get(f"{base}/api/query?q={q}")
    data = json.loads(body or b"{}")
    path = data.get("path") or {}
    hops = path.get("hops")
    ok = code == 200 and isinstance(hops, int) and hops >= 1
    check(ok, "多跳路径", f"HTTP {code}, hops={hops}, intent={data.get('intent')}")

    code, body = post_json(base + "/api/query", {"question": "随机森林; MATCH (n) DETACH DELETE n"})
    check(code in (400, 422), "注入拦截", f"HTTP {code}")

    print(f"===== 自检结论：{'全部通过 ✅' if fails == 0 else f'{fails} 项失败 ❌'} =====")
    return fails


def main() -> None:
    if "--verify" in sys.argv:
        idx = sys.argv.index("--verify")
        sys.exit(1 if verify(sys.argv[idx + 1].rstrip("/")) else 0)

    load_local_env()
    env = require_env()
    print("[0/4] 部署前检查")
    print(f"      NEO4J_URI = {env['NEO4J_URI']}")
    ensure_project()
    print("[ENV] 写入 Vercel 环境变量（数据库凭据不上传到仓库，只存在项目设置里）")
    push_env(env)

    url = deploy()
    print(f"\n线上地址：{url}")
    sys.exit(1 if verify(url) else 0)


if __name__ == "__main__":
    main()
