#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""频域水印抗裁切测试：验证内容重复3次+投票合并后的检测率。"""
import os
import sys
import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.algorithms.frequency_dct import (
    FrequencyDctWatermark, render_text_wm_fixed,
    _read_image, _write_image, REF_LONG, WM_W, WM_H
)
from app.models import WatermarkProfile

TEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_crop")
os.makedirs(TEST_DIR, exist_ok=True)

KEY = "test-key-123"
CONTENT = "内部文件A"
STRENGTH = 40


def make_test_image(w=1200, h=800):
    """生成一张有丰富纹理的测试图（渐变+噪声+文字），模拟真实照片。"""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    # 渐变背景
    for y in range(h):
        for x in range(w):
            img[y, x] = [
                int(100 + 80 * x / w),
                int(80 + 60 * y / h),
                int(120 + 50 * (x + y) / (w + h))
            ]
    # 添加随机噪声（增加纹理，避免纯色图DCT系数过小）
    noise = np.random.randint(-15, 15, img.shape, dtype=np.int16)
    img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    # 添加一些方块图案
    for i in range(5):
        x0 = np.random.randint(50, w - 200)
        y0 = np.random.randint(50, h - 200)
        color = tuple(np.random.randint(50, 200, 3).tolist())
        cv2.rectangle(img, (x0, y0), (x0 + 150, y0 + 100), color, -1)
    return img


def crop_center(img, ratio):
    """中心裁切，保留 ratio 比例的面积。"""
    h, w = img.shape[:2]
    new_h = int(h * np.sqrt(ratio))
    new_w = int(w * np.sqrt(ratio))
    y0 = (h - new_h) // 2
    x0 = (w - new_w) // 2
    return img[y0:y0+new_h, x0:x0+new_w]


def crop_asymmetric(img, left_ratio, top_ratio):
    """非对称裁切：裁掉左边 left_ratio，上边 top_ratio。"""
    h, w = img.shape[:2]
    x0 = int(w * left_ratio)
    y0 = int(h * top_ratio)
    return img[y0:, x0:]


def test_detect(alg, img_path, label):
    """测试检测，返回 (是否命中, 内容匹配度, magic_err)。"""
    profile = WatermarkProfile(
                name="test", algorithm="frequency_dct",
                owner=CONTENT,
                extra={"key": KEY, "content": CONTENT, "strength": STRENGTH,
                       "cand_contents": [CONTENT]})
    result = alg.verify(img_path, profile)
    if result and result.get("detected"):
        sim = result.get("similarity", 0)
        matched = result.get("matched_content")
        print(f"  [{label}] ✅ 命中 | 相似度={sim:.3f} | 匹配内容='{matched}' | 候选尺度={result.get('cand_size')}")
        return True, sim, 0
    else:
        print(f"  [{label}] ❌ 未命中")
        return False, 0, 99


def main():
    print("=" * 70)
    print("  频域水印抗裁切测试（内容重复3次 + 投票合并）")
    print("=" * 70)

    # 1. 生成测试图并嵌入水印
    print("\n[1/4] 生成测试图片并嵌入频域水印...")
    original = make_test_image(1200, 800)
    src_path = os.path.join(TEST_DIR, "original.png")
    _write_image(src_path, original)

    alg = FrequencyDctWatermark()
    profile = WatermarkProfile(
                name="test", algorithm="frequency_dct",
                owner=CONTENT,
                extra={"key": KEY, "content": CONTENT, "strength": STRENGTH})
    wm_path = os.path.join(TEST_DIR, "watermarked.png")
    info = alg.embed(src_path, wm_path, profile)
    print(f"  嵌入完成: 块数={info['blocks']}, delta={info['delta']:.1f}")

    # 2. 正常提取
    print("\n[2/4] 正常提取（无裁切）...")
    test_detect(alg, wm_path, "原图")

    # 3. 中心裁切测试
    print("\n[3/4] 中心裁切测试...")
    wm_img = _read_image(wm_path)
    for ratio in [0.75, 0.50, 0.25, 0.10]:
        cropped = crop_center(wm_img, ratio)
        crop_path = os.path.join(TEST_DIR, f"crop_center_{int(ratio*100)}.png")
        _write_image(crop_path, cropped)
        test_detect(alg, crop_path, f"中心裁切{int(ratio*100)}%")

    # 4. 非对称裁切测试
    print("\n[4/4] 非对称裁切测试（裁掉左边+上边）...")
    for left, top in [(0.1, 0.1), (0.2, 0.2), (0.3, 0.3), (0.25, 0.1)]:
        cropped = crop_asymmetric(wm_img, left, top)
        crop_path = os.path.join(TEST_DIR, f"crop_asym_L{int(left*100)}_T{int(top*100)}.png")
        _write_image(crop_path, cropped)
        test_detect(alg, crop_path, f"左{int(left*100)}%+上{int(top*100)}%")

    print("\n" + "=" * 70)
    print("  测试完成！测试图片保存在:", TEST_DIR)
    print("=" * 70)


if __name__ == "__main__":
    main()
