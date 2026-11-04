#!/bin/zsh
# ---------------------------------------------------------------------------
# 一键启动脚本：机器学习知识图谱系统
# 用法：  ./start.sh             前台运行，Ctrl+C 停止
#         PORT=8010 ./start.sh   换端口（8000 被占用时）
# 说明：  首次运行会自动创建 .venv 并安装依赖；之后直接启动。
# ---------------------------------------------------------------------------
cd "$(dirname "$0")" || exit 1

PORT="${PORT:-8000}"

# 1. 虚拟环境不存在 → 创建并安装依赖（只发生在第一次）
if [ ! -x ".venv/bin/python" ]; then
  echo "[1/2] 首次运行：创建虚拟环境并安装依赖（约 1 分钟）…"
  python3 -m venv .venv || exit 1
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt || exit 1
  echo "      依赖安装完成。"
fi

# 2. 端口占用检查：提前说清楚，避免看到一屏 ERROR 不知所措
if lsof -ti :"${PORT}" >/dev/null 2>&1; then
  echo "端口 ${PORT} 已被占用，可能是服务已经在运行。"
  echo "  · 直接打开浏览器：http://127.0.0.1:${PORT}/"
  echo "  · 或者换端口启动：PORT=$((PORT + 10)) ./start.sh"
  echo "  · 想强制重启，先结束旧进程：kill \$(lsof -ti :${PORT})"
  exit 1
fi

# 3. Neo4j 未运行 → 自动拉起本机安装的 Neo4j 4.4（可选，失败则自动降级本地图）
NEO4J_HOME_DIR="${HOME}/neo4j-community-4.4.48"
if [ -x "${NEO4J_HOME_DIR}/bin/neo4j" ] && [ -d "${HOME}/jdk11/Contents/Home" ]; then
  if ! nc -z localhost 7687 >/dev/null 2>&1; then
    echo "[+] 检测到 Neo4j 未运行，正在启动（约 15 秒）…"
    JAVA_HOME="${HOME}/jdk11/Contents/Home" "${NEO4J_HOME_DIR}/bin/neo4j" start \
      >/tmp/neo4j-startup.log 2>&1 \
      && echo "    Neo4j 已启动（bolt://localhost:7687，角标显示已连接）" \
      || echo "    Neo4j 启动失败（见 /tmp/neo4j-startup.log），系统将使用本地图模式，功能不受影响"
  fi
fi

# 4. 启动服务
echo "[2/2] 启动服务：http://127.0.0.1:${PORT}/  （Ctrl+C 停止）"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}"
