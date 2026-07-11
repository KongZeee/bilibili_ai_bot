"""
BiliBot 入口

可以通过以下方式运行：
    python -m bilibot
    python -m bilibot --config config.yaml
    python -m bilibot --quickstart
"""
from .app.app import main

if __name__ == "__main__":
    main()