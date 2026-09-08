# -*- coding: utf-8 -*-
"""批量处理：文件夹遍历、批量加水印、批量验证、结果汇总。"""
import os
import tempfile

from .config import IMAGE_EXTS
from .models import FileResult, WatermarkProfile


def _as_list(x):
    """兼容单个档位或档位列表。"""
    return x if isinstance(x, (list, tuple)) else [x]


def apply_profiles(engine, src_path: str, out_path: str, profiles) -> object:
    """按顺序把多个水印档位应用到同一张图（如：频域水印 + 标记水印 双保险）。

    注意顺序：频域（改像素）在前、标记（字节追加）在后，避免后处理把前面抹掉。
    """
    profiles = _as_list(profiles)
    if not profiles:
        raise ValueError("至少选择一种水印")
    if len(profiles) == 1:
        return engine.embed(src_path, out_path, profiles[0])

    ext = os.path.splitext(out_path)[1] or ".png"
    tmp = tempfile.mktemp(suffix=ext, dir=os.path.dirname(os.path.abspath(out_path)) or ".")
    cur = src_path
    detail = None
    try:
        for prof in profiles:
            detail = engine.embed(cur, tmp, prof)
            cur = tmp
        os.replace(tmp, out_path)
        return {"profiles": [p.name for p in profiles], "last_detail": detail}
    finally:
        if os.path.exists(tmp) and os.path.abspath(tmp) != os.path.abspath(out_path):
            try:
                os.remove(tmp)
            except OSError:
                pass


def scan_images(folder: str, exts=None) -> list:
    """递归收集文件夹下所有支持的图片文件，返回相对路径列表。"""
    exts = exts or IMAGE_EXTS
    found = []
    for root, _dirs, files in os.walk(folder):
        for fn in sorted(files):
            if os.path.splitext(fn)[1].lower() in exts:
                rel = os.path.relpath(os.path.join(root, fn), folder)
                found.append(rel)
    return found


def batch_embed(engine, input_dir: str, output_dir: str, profiles, 
                overwrite: bool = False, exts=None) -> list:
    """批量加水印：按输入目录的相对结构输出到 output_dir，返回 FileResult 列表。

    profiles：单个档位或档位列表（列表则叠加应用，如频域+标记双保险）。
    """
    profiles = _as_list(profiles)
    results = []
    for rel in scan_images(input_dir, exts):
        src = os.path.join(input_dir, rel)
        dst = os.path.join(output_dir, rel)
        if os.path.exists(dst) and not overwrite:
            results.append(FileResult(rel, False, "输出已存在，跳过（加 --overwrite 覆盖）"))
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            detail = apply_profiles(engine, src, dst, profiles)
            results.append(FileResult(rel, True, "OK", detail))
        except Exception as e:
            results.append(FileResult(rel, False, f"失败: {e}"))
    return results


def batch_verify(engine, folder: str, profile=None, exts=None) -> list:
    """批量验证文件夹里每张图片是否带我们的水印，返回 FileResult 列表。

    profile：验证时核对的档位（频域水印必需，文件级标记可省略）。
    """
    results = []
    for rel in scan_images(folder, exts):
        path = os.path.join(folder, rel)
        try:
            r = engine.verify(path, profile=profile)
            if r is None:
                results.append(FileResult(rel, False, "无水印"))
            elif r.get("tampered"):
                results.append(FileResult(rel, False, "标记被改动", r))
            else:
                results.append(FileResult(rel, True, "有水印", r))
        except Exception as e:
            results.append(FileResult(rel, False, f"失败: {e}"))
    return results


def summarize(results: list) -> tuple:
    """返回 (成功数, 失败数, 总数)。"""
    ok = sum(1 for r in results if r.ok)
    return ok, len(results) - ok, len(results)
