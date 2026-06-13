#!/bin/bash
# GDUST 课表抓取工具 — 一键启动脚本
# 用法: ./start.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "🎓 GDUST 课表抓取工具"
echo "─────────────────────"

# 检查 Python
if ! command -v python3 &>/dev/null; then
    echo "❌ 未找到 python3，请先安装 Python 3.9+"
    exit 1
fi

PYTHON_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "✅ Python $PYTHON_VER"

# 检查 & 安装依赖
export PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
export PIP_TRUSTED_HOST="pypi.tuna.tsinghua.edu.cn"

echo "📦 检查依赖..."
python3 -c "import flask" 2>/dev/null || {
    echo "   安装 flask..."
    pip3 install flask -q
}
python3 -c "import requests" 2>/dev/null || {
    echo "   安装 requests..."
    pip3 install requests -q
}
python3 -c "import ddddocr" 2>/dev/null && echo "✅ ddddocr 已安装（自动验证码可用）" || echo "⚠️  ddddocr 未安装（可选，不影响使用）"

# 检查 config.json
if [ ! -f "config.json" ]; then
    if [ -f "config.example.json" ]; then
        echo "📋 首次运行，复制配置模板..."
        cp config.example.json config.json
        echo "   请编辑 config.json 填写学号等信息，或在 Web 界面中填写"
    fi
fi

# 启动
PORT=${1:-5000}
echo ""
echo "🌐 启动 Web GUI..."
echo "📎 浏览器打开: http://localhost:$PORT"
echo "   按 Ctrl+C 停止"
echo ""

python3 app.py
