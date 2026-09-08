# -*- coding: utf-8 -*-
"""图形界面入口。

在项目根目录、venv 已激活下运行：
    python gui.py          # 打开图形界面
    python gui.py --smoke  # 自检模式：窗口约 2 秒后自动关闭
"""
import sys

from app.gui import run

if __name__ == "__main__":
    run(smoke="--smoke" in sys.argv)
