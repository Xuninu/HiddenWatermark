# -*- coding: utf-8 -*-
"""方案二：文件级隐藏标记。

原理：不动图片任何像素，把标记信息以二进制形式"追加"到文件末尾。
大多数看图软件/浏览器会忽略尾部数据，所以图片看起来完全一样，
且原始字节 100% 保留（追加块不影响原文件任何数据）。

标记块布局（追加在文件末尾）：
    [payload(JSON文本)] [4字节长度] [32字节sha256校验] [8字节魔数 HWM1HWM1]
"""
import hashlib
import json
import time

from .base import WatermarkAlgorithm

MAGIC = b"HWM1HWM1"               # 固定魔数：识别"这是我们的水印"
LEN_SIZE = 4
HASH_SIZE = 32


class DataAppendWatermark(WatermarkAlgorithm):
    """文件级追加标记水印。任何文件格式都适用。"""

    name = "data_append"

    def _make_block(self, info: dict) -> bytes:
        if "ts" not in info:
            info["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        payload = json.dumps(info, ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(payload).digest()
        length = len(payload).to_bytes(LEN_SIZE, "big")
        return payload + length + digest + MAGIC

    def embed(self, src_path: str, out_path: str, profile) -> object:
        info = {"owner": profile.owner, "note": profile.note}
        for k, v in (profile.extra or {}).items():     # 允许配置带额外字段
            if k not in info:
                info[k] = v
        with open(src_path, "rb") as f:
            data = f.read()
        block = self._make_block(info)
        with open(out_path, "wb") as f:
            f.write(data + block)
        return {"appended_bytes": len(block)}

    def verify(self, path: str, profile=None):
        with open(path, "rb") as f:
            data = f.read()
        footer = HASH_SIZE + LEN_SIZE + len(MAGIC)
        if len(data) < footer:
            return None

        # 在全文件中找最后一次出现魔数的位置，向前解析。
        # 好处：即使有人在标记后又追加了数据，也能把"被盖住的标记"找出来。
        pos = data.rfind(MAGIC)
        if pos < 0:
            return None

        length_start = pos - HASH_SIZE - LEN_SIZE
        if length_start < 0:
            return None
        length = int.from_bytes(data[length_start:length_start + LEN_SIZE], "big")
        payload_start = length_start - length
        if payload_start < 0:
            return None

        payload_bytes = data[payload_start:length_start]
        digest = data[length_start + LEN_SIZE:pos]
        covered = (pos + len(MAGIC)) != len(data)

        if hashlib.sha256(payload_bytes).digest() != digest:
            return {"tampered": True, "reason": "校验和不匹配，标记内容可能被改动过"}

        try:
            info = json.loads(payload_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"tampered": True, "reason": "标记内容无法解析"}
        info["_verified"] = True
        if covered:
            info["_covered"] = True
            info["_reason"] = "标记仍可读取，但其后存在额外数据（可能被二次追加/覆盖）"
        return info
