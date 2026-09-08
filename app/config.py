# -*- coding: utf-8 -*-
"""配置加载：预设水印档位（profiles.json）、支持的图片格式。"""
import json
import os
import sys

from .models import WatermarkProfile

# 默认预设档位：首次运行会自动生成 profiles.json（记得把 owner 改成你自己的名字）
DEFAULT_PROFILES = [
    {"name": "个人专属", "algorithm": "data_append", "owner": "我的名字", "note": ""},
    {"name": "团队标准", "algorithm": "data_append", "owner": "我的团队", "note": "内部发布"},
    {"name": "公开渠道", "algorithm": "data_append", "owner": "我的团队", "note": "公开渠道"},
    {"name": "抗篡改(频域)", "algorithm": "frequency_dct", "owner": "我的团队",
     "note": "抗压缩/重编码", "extra": {"key": "my-secret-key", "strength": 40}},
]

# 批量处理时只处理这些图片格式
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp", ".tga"}


def profiles_path() -> str:
    """profiles.json 的位置。

    - 源码运行时：放在项目根目录（与 main.py 同级），方便直接改；
    - 打包成 exe 后：程序目录只读，改放到 %APPDATA%\\HiddenWatermark 下。
    """
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        folder = os.path.join(base, "HiddenWatermark")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, "profiles.json")
    here = os.path.dirname(os.path.abspath(__file__))       # .../app
    return os.path.join(os.path.dirname(here), "profiles.json")


def ensure_profiles_file(path: str = None) -> str:
    """配置不存在时自动生成一份默认配置。"""
    path = path or profiles_path()
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"profiles": DEFAULT_PROFILES}, f, ensure_ascii=False, indent=2)
        print(f"[配置] 已生成默认配置: {path}（请把 owner 改成你的名字）")
    return path


def load_profiles(path: str = None) -> list:
    """读取预设水印档位，返回 WatermarkProfile 列表。"""
    path = ensure_profiles_file(path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [WatermarkProfile.from_dict(d) for d in data.get("profiles", [])]
