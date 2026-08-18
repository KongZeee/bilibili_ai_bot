#!/bin/bash
# BiliBot 启动脚本

cd "$(dirname "$0")"

# 运行
python -m bilibot "$@"
