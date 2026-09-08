# -*- coding: utf-8 -*-
"""算法注册表：所有水印算法在这里注册，引擎按 name 查找。

新增算法：在下方 import 并 register 一行即可，无需改其他代码。
"""
from .base import WatermarkAlgorithm
from .data_append import DataAppendWatermark
from .frequency_dct import FrequencyDctWatermark

_ALGORITHMS = {}


def register(algo: WatermarkAlgorithm) -> WatermarkAlgorithm:
    _ALGORITHMS[algo.name] = algo
    return algo


def get_algorithm(name: str) -> WatermarkAlgorithm:
    if name not in _ALGORITHMS:
        raise KeyError(f"未知水印算法: {name}，当前可用: {available_algorithms()}")
    return _ALGORITHMS[name]


def available_algorithms() -> list:
    return list(_ALGORITHMS)


register(DataAppendWatermark())
register(FrequencyDctWatermark())
