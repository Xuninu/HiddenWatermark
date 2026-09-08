# -*- coding: utf-8 -*-
"""数据模型：水印档位、单个文件处理结果。"""
from dataclasses import dataclass, field


@dataclass
class WatermarkProfile:
    """一个预设的水印档位。

    algorithm 指定用哪种算法；extra 是各算法私有参数（方案一接入后，
    例如强度、密钥等都可以放这里），框架层不关心具体含义。
    """
    name: str = "默认"
    algorithm: str = "data_append"
    owner: str = ""
    note: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "WatermarkProfile":
        d = dict(d or {})
        extra = d.pop("extra", {}) or {}
        return cls(
            name=str(d.get("name", "默认")),
            algorithm=str(d.get("algorithm", "data_append")),
            owner=str(d.get("owner", "")),
            note=str(d.get("note", "")),
            extra=dict(extra),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "algorithm": self.algorithm,
            "owner": self.owner,
            "note": self.note,
            "extra": self.extra,
        }


@dataclass
class FileResult:
    """单个文件的处理/验证结果。"""
    path: str
    ok: bool
    message: str = ""
    detail: object = None   # 算法返回的额外信息（追加字节数 / 验证信息等）
