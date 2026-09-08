# -*- coding: utf-8 -*-
"""水印算法抽象基类。

所有水印算法实现同一个接口（embed / verify），引擎只认这个接口。
以后接入方案一（频域水印）只需：
  1. 继承本类实现两个方法；
  2. 在 app/algorithms/__init__.py 里注册。
"""
from abc import ABC, abstractmethod


class WatermarkAlgorithm(ABC):
    """水印算法接口。"""

    name: str = "base"

    @abstractmethod
    def embed(self, src_path: str, out_path: str, profile) -> object:
        """给 src_path 打水印并写出到 out_path。返回额外信息 dict（如追加字节数）。"""

    @abstractmethod
    def verify(self, path: str, profile=None):
        """检测 path 是否带本算法水印。

        profile：可选的验证档位。频域水印需要用它核对密钥/归属人/强度；
                文件级标记等算法可忽略。
        返回：
          - 有水印且校验通过：dict
          - 有水印但被改动：dict（含 tampered=True）
          - 没有水印：None
        """
