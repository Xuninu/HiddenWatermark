# -*- coding: utf-8 -*-
"""HiddenWatermark 软件入口。

在项目根目录、venv 已激活下运行：
    python main.py profiles
    python main.py embed 原图.png 输出.png --profile 个人专属
    python main.py verify 图片.png
    python main.py batch-embed 输入文件夹 输出文件夹 --profile 团队标准
    python main.py batch-verify 文件夹
"""
from app.cli import main

if __name__ == "__main__":
    main()
