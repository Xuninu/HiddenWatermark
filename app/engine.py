# -*- coding: utf-8 -*-
"""水印引擎：统一入口，负责算法分发与多算法验证。"""
from . import algorithms
from .models import WatermarkProfile


class WatermarkEngine:
    """对外只暴露 embed / verify 两个动作，内部按算法名分发。"""

    def embed(self, src_path: str, out_path: str, profile: WatermarkProfile) -> object:
        algo = algorithms.get_algorithm(profile.algorithm)
        return algo.embed(src_path, out_path, profile)

    def verify(self, path: str, algorithm: str = None, profile=None):
        """验证单个文件。

        - 指定 algorithm：只查该算法；
        - 指定 profile：按该档位指定的算法及其参数（密钥/强度/归属人）核验；
        - 都不指定：按顺序尝试所有已注册算法（主要用于自动识别文件级标记）。
        返回结果 dict（带 _algorithm 字段标明是哪个算法找到的），未命中返回 None。
        """
        if algorithm:
            return algorithms.get_algorithm(algorithm).verify(path, profile)
        if profile:
            result = algorithms.get_algorithm(profile.algorithm).verify(path, profile)
            if result is not None:
                result["_algorithm"] = profile.algorithm
            return result
        for name in algorithms.available_algorithms():
            result = algorithms.get_algorithm(name).verify(path, profile=profile)
            if result is not None:
                result["_algorithm"] = name
                return result
        return None
