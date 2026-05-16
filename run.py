#!/usr/bin/env python3
"""
FileID Bot - 受保护版本启动入口
此文件是唯一保留的 .py 文件，仅负责启动编译后的模块。
"""
import sys
import os

# 确保当前目录在 Python 路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import main

if __name__ == "__main__":
    main()
