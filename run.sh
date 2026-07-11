#!/bin/bash
# BiliBot 启动脚本

cd "$(dirname "$0")"

# 设置 PYTHONPATH
export PYTHONPATH="$(pwd)/standalone:$PYTHONPATH"

# 运行
python -m bilibot "$@"