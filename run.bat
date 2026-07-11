# BiliBot 入口脚本

# 从 standalone 版本运行（兼容）
set PYTHONPATH=%CD%\standalone;%PYTHONPATH%

# 运行 bilibot
python -m bilibot %*