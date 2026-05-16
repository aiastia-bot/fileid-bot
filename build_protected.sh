#!/usr/bin/env bash
# ============================================================
# FileID Bot - Nuitka 编译脚本
# 将所有 .py 编译为 .so/.pyd 二进制模块
# 用法: bash build_protected.sh [linux|windows]
# ============================================================
set -euo pipefail

PLATFORM="${1:-linux}"
PYTHON="python3"

echo "=========================================="
echo "  FileID Bot - Nuitka Protected Build"
echo "  Platform: $PLATFORM"
echo "=========================================="

# 检查 Nuitka 是否安装
if ! $PYTHON -m nuitka --version &> /dev/null; then
    echo "❌ Nuitka 未安装，正在安装..."
    $PYTHON -m pip install nuitka ordered-set zstandard
fi

# 公共 Nuitka 编译参数
# --nofollow-imports: 不编译第三方库（只编译指定的模块）
# --module: 编译为可导入的 .so/.pyd 扩展模块
# --remove-output: 清理中间 .build 目录
COMMON_FLAGS=(
    --module
    --nofollow-imports
    --assume-yes-for-downloads
    --remove-output
    --no-progressbar
)

# Linux 特有参数
if [ "$PLATFORM" = "linux" ]; then
    COMMON_FLAGS+=(--lto=yes)
fi

# 输出目录（也是最终分发包的基础）
BUILD_DIR="dist_protected"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

echo ""
echo "📦 开始编译模块..."
echo ""

# ============ 顶层单文件模块 ============
TOP_MODULES=(
    "main.py"
    "config.py"
    "bot_manager.py"
    "redis_manager.py"
    "scheduler.py"
    "send_queue.py"
    "senders.py"
    "utils.py"
    "webhook_server.py"
    "worker_server.py"
)

for mod in "${TOP_MODULES[@]}"; do
    if [ -f "$mod" ]; then
        echo "🔧 编译: $mod"
        $PYTHON -m nuitka "${COMMON_FLAGS[@]}" \
            --output-dir="$BUILD_DIR" \
            "$mod" 2>&1
        echo "✅ 完成: $mod"
        echo ""
    else
        echo "⚠️ 跳过（不存在）: $mod"
    fi
done

# ============ 包目录 ============
PACKAGES=(
    "db"
    "handlers"
)

for pkg in "${PACKAGES[@]}"; do
    if [ -d "$pkg" ]; then
        echo "🔧 编译包: $pkg/"
        $PYTHON -m nuitka "${COMMON_FLAGS[@]}" \
            --output-dir="$BUILD_DIR" \
            "$pkg" 2>&1
        echo "✅ 完成包: $pkg/"
        echo ""
    else
        echo "⚠️ 跳过（不存在）: $pkg/"
    fi
done

echo "=========================================="
echo "  📋 组装受保护的分发包..."
echo "=========================================="

# 最终分发目录
DIST_DIR="fileid-bot-protected"
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

# 复制编译后的所有内容（Nuitka --module 已保持正确的目录结构）
# 顶层 .so 文件
find "$BUILD_DIR" -maxdepth 1 -name "*.so" -exec cp {} "$DIST_DIR/" \; 2>/dev/null || true
find "$BUILD_DIR" -maxdepth 1 -name "*.pyd" -exec cp {} "$DIST_DIR/" \; 2>/dev/null || true

# db/ 包（编译后的 .so 文件已包含 __init__.so）
if [ -d "$BUILD_DIR/db" ]; then
    mkdir -p "$DIST_DIR/db"
    cp -r "$BUILD_DIR/db/"* "$DIST_DIR/db/" 2>/dev/null || true
fi

# handlers/ 包
if [ -d "$BUILD_DIR/handlers" ]; then
    mkdir -p "$DIST_DIR/handlers"
    cp -r "$BUILD_DIR/handlers/"* "$DIST_DIR/handlers/" 2>/dev/null || true
fi

# handlers/master/ 子包
if [ -d "$BUILD_DIR/handlers/master" ]; then
    mkdir -p "$DIST_DIR/handlers/master"
    cp -r "$BUILD_DIR/handlers/master/"* "$DIST_DIR/handlers/master/" 2>/dev/null || true
fi

# 复制启动入口和配置文件
cp run.py "$DIST_DIR/"
cp requirements.txt "$DIST_DIR/"
cp .env.example "$DIST_DIR/" 2>/dev/null || true

# 创建数据目录
mkdir -p "$DIST_DIR/data"

echo ""
echo "=========================================="
echo "  ✅ 编译完成！"
echo "  分发包位于: $DIST_DIR/"
echo ""
echo "  文件列表:"
find "$DIST_DIR" -type f | sort
echo ""
echo "  文件大小:"
du -sh "$DIST_DIR"
echo "=========================================="
