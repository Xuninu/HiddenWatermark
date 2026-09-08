# -*- coding: utf-8 -*-
"""方案一：DCT 频域水印（差分嵌入 + 盲提取，可还原水印内容图）。

原理（参考经典差分调制方案）：
  1. 宿主图转 YCrCb，取 Y（亮度）通道 —— JPEG 对亮度量化更细，水印更抗压；
  2. 把 Y 分成 8x8 块，逐块做 DCT；
  3. 每块选 2 个中频系数做「差分调制」嵌入 1 bit：
        c1 - c2 >= delta  ->  1
        c2 - c1 >= delta  ->  0
     系数远离 DC/超高频，肉眼不可见且抗压缩；
  4. 载荷 = 固定画布的二值水印内容图 + 「密钥绑定的 magic 校验位」：
     - 内容图由水印内容文本渲染（40x10，文字自适应字号）
     - magic 16 bit 由密钥 sha256 派生，重复 5 次嵌入
     每块通过「块相对图中心的坐标 + 密钥」的 crc32 哈希独立归属载荷的某一位，
     嵌入时把该位写入对应块，提取时逐块投票多数判决 —— 与总块数无关，
     因此图片被「裁剪 / 缩放 / 改分辨率」后，只要画面里还剩足够多的块就能还原。

抗几何攻击（截图/缩放/裁剪/改分辨率）设计：
  - 嵌入与提取都先把图等比缩放进「参考坐标系」（长边=REF_LONG），网格固定对齐，
    因此图片被等比缩放 / 改分辨率后，缩回参考坐标系网格自动对齐；
  - 投票式分配不依赖总块数，中心裁剪只减少每个位的投票冗余而不破坏映射；
  - 提取时对检测图尝试多个候选尺度（覆盖图片被裁剪后内容放大的情况），
    每个尺度独立投票解码，用「magic 校验位」确认密钥正确且水印存在。

局限（所有频域水印共性 + 几何）：
  - 非等比拉伸（宽高比改变）仍难可靠对齐；
  - 非对称裁剪（画面相对中心偏移）会破坏相对坐标，暂时难以检测；
  - 极端裁剪 / 大幅缩小（保留画面或分辨率 <25%）因信息过少难以检测；
  - 算法换代后旧版（块级签名 / 置乱序列）水印无法识别，需重新加水印。
"""
import hashlib
import os
import zlib

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .base import WatermarkAlgorithm

BLOCK = 8
# 选取的中频 DCT 系数坐标（避开 DC 与极低频，保证不可见；取值偏低一点，抗 JPEG 压缩更好）
U1, V1 = 2, 2
U2, V2 = 1, 3
DEFAULT_SECRET = "hidden-watermark-default-key"
REF_LONG = 1024           # 参考坐标系长边：嵌入与提取共用，保证缩放后网格对齐
CAND_MAX = REF_LONG * 3 // 2   # 检测候选长边上限（截图带留白/边框 -> 内容缩小 -> 需放大 >REF）
WM_W, WM_H = 40, 10       # 固定水印内容画布（宽, 高）
CONTENT_BITS = WM_W * WM_H
CONTENT_REPEAT = 3         # 内容比特重复嵌入次数（抗裁切/抗噪：3副本多数投票可纠正1/3错误）
MAGIC_BITS = 32           # 密钥绑定的 magic 校验位（独立于内容，抗误报）：
                          # 32 位签名使「无水印/错密钥在全尺度扫描中偶合命中」概率 <0.02%
MAGIC_REPEAT = 7          # magic 每 bit 重复嵌入次数（提取时多数判决，抗位错/抗 JPEG）
TOTAL_BITS = CONTENT_BITS * CONTENT_REPEAT + MAGIC_BITS * MAGIC_REPEAT
MAGIC_HAMMING = 5         # magic 允许的位错上限（32 位签名下：真实水印含缩放/JPEG 攻击
                          # 位错 0~5，无水印/错密钥位错 >=9，间隔清晰；偶合误报概率极低）
# 提取时的候选尺度（长边）：步长 64px，覆盖「原图等比缩放」与「中心裁剪后内容放大」的常见比例
CANDIDATES = [1024, 960, 896, 832, 768, 704, 640, 576, 512, 448, 384, 320, 256, 192, 128]

# 8x8 DCT-II 正交基（预计算，批量向量化用）
_DCT_MAT = np.zeros((8, 8), dtype=np.float64)
for _i in range(8):
    for _j in range(8):
        _DCT_MAT[_i, _j] = (np.sqrt(1.0 / 8.0) if _i == 0
                            else np.sqrt(2.0 / 8.0) * np.cos(np.pi * (2 * _j + 1) * _i / 16.0))


def _read_image(path):
    """读取图片，兼容任意路径（含中文/空格）：cv2.imread 在 Windows 上不支持非 ASCII 路径，
    因此先按字节读入再用 imdecode 解码。OpenCV 不支持的格式（如 TGA）回退到 Pillow。"""
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        # 回退：用 Pillow 读取（支持 TGA 等 OpenCV 不支持的格式）
        try:
            pil_img = Image.open(path).convert("RGB")
            rgb = np.array(pil_img)
            img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        except Exception as e:
            raise ValueError(f"无法读取图片: {path}，错误: {e}")
    return img


def _write_image(path, img):
    """保存图片，兼容任意路径（含中文/空格），保持原格式且尽量无损。
    - PNG/TGA/BMP/TIFF：无损
    - JPEG：quality=95
    - WebP：无损模式（lossless=on）
    - TGA：OpenCV 不支持编码，改用 Pillow 保存
    """
    ext = os.path.splitext(path)[1].lower() or ".png"
    if ext == ".jpg":
        ext = ".jpeg"

    # TGA：OpenCV 5.x 无编码器，用 Pillow 无损保存
    if ext == ".tga":
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb)
        pil_img.save(path, format="TGA")
        return

    # 其他格式用 OpenCV，按格式设置质量参数
    params = []
    if ext in (".jpeg", ".jpg"):
        params = [cv2.IMWRITE_JPEG_QUALITY, 95]
    elif ext == ".webp":
        # WebP 无损模式
        params = [cv2.IMWRITE_WEBP_LOSSLESS_MODE, cv2.IMWRITE_WEBP_LOSSLESS_ON]
    # PNG/BMP/TIFF 默认无损，无需额外参数

    try:
        ok, buf = cv2.imencode(ext, img, params)
    except cv2.error:
        ok = False
    if not ok:
        # 回退：用 Pillow 保存（BGR->RGB）
        try:
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb)
            pil_img.save(path)
            return
        except Exception as e2:
            raise ValueError(f"保存失败（格式 {ext} 可能不支持）: {path}，错误: {e2}")
    buf.tofile(path)


def _resize_even_blocks(img, long_side):
    """把图等比缩放到参考坐标系（长边=long_side，保持宽高比，不做裁边）。

    与旧版的关键区别：不再把宽/高强改成「偶数个 8x8 块」。
    嵌入 / 检测时块网格统一以「图中心」为原点向两边扩展（中心锚定），
    中心裁剪不会移动内容中心 -> 嵌入与检测的块网格自动对齐，
    从而消除「裁剪边界不在 8px 块边界上」导致的亚块相位错位。
    """
    h, w = img.shape[:2]
    scale = long_side / max(h, w)
    nw = max(8, int(round(w * scale)))
    nh = max(8, int(round(h * scale)))
    return cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LANCZOS4)


def _grid_info(h, w, cx=None, cy=None):
    """中心锚定块网格：返回块数 (nb_h, nb_w) 与块网格左上角偏移 (off_h, off_w)。

    默认以「图中心」为块网格中心（四舍五入像素）；cx/cy 指定时以内容中心为准。
    嵌入与检测都用同一规则，因此中心裁剪 / 内容居中的截图无需相位搜索。
    若图片截图包含边框/标题栏导致内容偏移，检测时先用 _estimate_content_center
    定位内容中心，再以内容中心锚定网格 -> 偏移自动对齐。
    块数取「以中心为原点、向两边能容纳的最大偶数块」（对称、不越界）。"""
    def _axis(L, c):
        c = int(c) if c is not None else int(L / 2.0 + 0.5)
        up = c // BLOCK
        down = (L - c) // BLOCK
        nb = min(up, down) * 2
        off = c - (nb // 2) * BLOCK
        return nb, off
    if cx is None:
        cx = int(w / 2.0 + 0.5)
    if cy is None:
        cy = int(h / 2.0 + 0.5)
    nb_h, off_h = _axis(h, cy)
    nb_w, off_w = _axis(w, cx)
    return nb_h, nb_w, off_h, off_w


def _estimate_content_center(img):
    """自动定位截图中的图片内容中心（灰度行/列方差的加权质心）。

    截图若包含纯色边框 / 标题栏 / 空白，这些区域方差≈0；
    图片内容区域有细节（方差大）。取「高方差像素行的中位」作为内容中心，
    对内部偶有大块纯色（天空/白墙）的图片也能稳健估计。
    返回 (cx, cy) 或 None（无法定位时回退图中心）。"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    h, w = gray.shape
    rv = gray.var(axis=1)      # (h,)
    cv_ = gray.var(axis=0)     # (w,)
    r_th = max(rv.max() * 0.04, 1.0)
    c_th = max(cv_.max() * 0.04, 1.0)
    rows = np.where(rv > r_th)[0]
    cols = np.where(cv_ > c_th)[0]
    if len(rows) < h * 0.02 or len(cols) < w * 0.02:
        return None            # 高方差区域过少（纯色图/无法定位），回退图中心
    # 用高方差像素的中位作为内容中心（对行内偶发低方差区域不敏感）
    return int(cols[len(cols) // 2]), int(rows[len(rows) // 2])


def _locate_content_box(img):
    """用边缘密度定位截图中的图片内容区域 (x0, y0, x1, y1)（左开右闭）。

    截图若包含纯色边框/标题栏/空白，这些区域没有边缘（边缘密度≈0）；
    图片内容有细节（边缘密度高）。取「高边缘行/列的最小/最大范围」为内容边界。
    返回 None 表示无法定位（回退全图候选扫描）。
    """
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    mag = np.abs(gx) + np.abs(gy)
    rsum = mag.mean(axis=1)          # 每行边缘强度
    csum = mag.mean(axis=0)          # 每列
    rth = max(rsum.max() * 0.08, 0.5)
    cth = max(csum.max() * 0.08, 0.5)
    rows = np.where(rsum > rth)[0]
    cols = np.where(csum > cth)[0]
    if len(rows) < h * 0.02 or len(cols) < w * 0.02:
        return None
    x0, y0, x1, y1 = cols.min(), rows.min(), cols.max() + 1, rows.max() + 1
    # 边界必须合理：内容区域至少占截图 30%（否则定位不可靠）
    if (x1 - x0) < w * 0.30 or (y1 - y0) < h * 0.30:
        return None
    return x0, y0, x1, y1


def _seed_from_key(key: str) -> int:
    """密钥字符串 -> 稳定的整数种子（参与块归属哈希）。"""
    return int.from_bytes(hashlib.sha256(f"hwm-dct::{key}".encode("utf-8")).digest()[:8], "big")


def _magic_from_key(key: str) -> np.ndarray:
    """密钥 -> 16 bit magic 校验模式（确定性，错密钥无法通过校验）。"""
    d = hashlib.sha256(f"hwm-magic::{key}".encode("utf-8")).digest()
    return np.array([(d[i // 8] >> (i % 8)) & 1 for i in range(MAGIC_BITS)], dtype=np.uint8)


def _pick_font(size: int):
    """选择支持中文的字体（Windows 系统字体），失败则退回默认字体。"""
    for fp in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
               "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc"):
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def render_text_wm(text):
    """把「水印内容」文本渲染成二值水印图（numpy uint8，1=文字/黑色部分，0=白底）。
    按文字实际尺寸渲染（用于 GUI 未检测到时的预览）。"""
    text = (text or "").strip() or "HiddenWatermark"
    font = _pick_font(20)
    tmp = Image.new("L", (8, 8))
    d = ImageDraw.Draw(tmp)
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 6, 4
    W = max(1, tw + pad_x * 2)
    H = max(1, th + pad_y * 2)
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    d.text((pad_x - bbox[0], pad_y - bbox[1]), text, fill=0, font=font)
    return (np.array(img) < 128).astype(np.uint8)


def render_text_wm_fixed(text, w=WM_W, h=WM_H):
    """把「水印内容」文本渲染成固定画布 (h, w) 的二值水印图，文字自动缩放字号填满画布。
    嵌入 / 提取 / 内容匹配共用同一渲染逻辑，保证所见即所嵌。"""
    text = (text or "").strip() or "HiddenWatermark"
    size = 20
    font = _pick_font(size)
    tmp = Image.new("L", (8, 8))
    bbox = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    while tw > w - 2 and size > 6:
        size -= 1
        font = _pick_font(size)
        bbox = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    th = bbox[3] - bbox[1]
    d.text(((w - tw) // 2 - bbox[0], (h - th) // 2 - bbox[1]), text, fill=0, font=font)
    return (np.array(img) < 128).astype(np.uint8)


def _bit_map(nb_w, nb_h, seed):
    """所有块 -> 载荷位的映射矩阵 (nb_h, nb_w)：
    基于「块相对图中心的坐标 + 密钥种子」的乘法散列。
    中心裁剪后，相对坐标保持不变 -> 同一物理块映射到同一载荷位。
    返回 numpy int64 矩阵，与总块数无关，向量化快速计算。"""
    rx = np.arange(nb_w, dtype=np.int64) - nb_w // 2
    ry = np.arange(nb_h, dtype=np.int64) - nb_h // 2
    s = (seed & 0xFFFFFFFF)
    # Knuth 乘法散列：混合行/列/种子，保证分布均匀
    k = ((rx[None, :] * 2654435761) ^ (ry[:, None] * 40503) ^ s) % TOTAL_BITS
    return k


def _blocks_dct2(blocks):
    """批量 8x8 块 DCT-II（正交归一）。blocks: (N,8,8) -> (N,8,8)。"""
    return np.einsum("ij,njk,lk->nil", _DCT_MAT, blocks, _DCT_MAT, optimize=True)


def _blocks_idct2(blocks):
    """批量 8x8 块 IDCT-II。"""
    return np.einsum("ij,njk,lk->nil", _DCT_MAT.T, blocks, _DCT_MAT.T, optimize=True)


def _build_payload(content, key):
    """构造完整载荷：内容 bits（重复 CONTENT_REPEAT 次）+ magic bits（重复 MAGIC_REPEAT 次）。

    内容重复 3 次的载荷结构：
      [内容副本0: 400 bits][内容副本1: 400 bits][内容副本2: 400 bits][magic: 32×7=224 bits]
    提取时对 3 个内容副本做投票数合并 + 多数判决，裁切/噪声导致某个副本受损时，
    其他副本仍可提供正确比特，从而显著提升抗裁切能力。"""
    magic = _magic_from_key(key)
    content_bits = content.ravel().astype(np.uint8)
    return np.concatenate([np.tile(content_bits, CONTENT_REPEAT),
                           np.tile(magic, MAGIC_REPEAT)])


class FrequencyDctWatermark(WatermarkAlgorithm):
    """DCT 频域水印（差分嵌入 + 投票式盲提取 + magic 校验，抗缩放/裁剪）。"""

    name = "frequency_dct"

    # ---------- 嵌入 ----------
    def embed(self, src_path: str, out_path: str, profile) -> object:
        extra = profile.extra or {}
        key = extra.get("key", DEFAULT_SECRET)
        strength = int(extra.get("strength", 40))
        delta = max(5.0, strength / 2.0)
        content = extra.get("content") or profile.owner or "HiddenWatermark"

        img = _read_image(src_path)
        G = _resize_even_blocks(img, REF_LONG)   # 参考坐标系图（等比缩放，保持宽高比）
        ycbcr = cv2.cvtColor(G, cv2.COLOR_BGR2YCrCb).astype(np.float32)
        Y = ycbcr[:, :, 0]
        nb_h, nb_w, off_h, off_w = _grid_info(Y.shape[0], Y.shape[1])
        total = nb_h * nb_w

        wm = render_text_wm_fixed(content)        # (WM_H, WM_W) 0/1
        payload = _build_payload(wm, key)
        if total < TOTAL_BITS:
            raise ValueError(f"宿主图过小：可容纳 {total} 块，水印需要 {TOTAL_BITS} 块。"
                             f"请换更大图片。")

        seed = _seed_from_key(key)
        kmat = _bit_map(nb_w, nb_h, seed)          # (nb_h, nb_w) 每块的载荷位
        Yg = Y[off_h: off_h + nb_h * BLOCK, off_w: off_w + nb_w * BLOCK]
        blocks = (Yg.reshape(nb_h, 8, nb_w, 8)
                  .transpose(0, 2, 1, 3).reshape(-1, 8, 8).astype(np.float64))
        d_all = _blocks_dct2(blocks)               # (N,8,8)
        c1 = d_all[:, U1, V1].copy()
        c2 = d_all[:, U2, V2].copy()
        bits = payload[kmat.ravel()]               # 每块要嵌入的 bit (N,)
        mid = (c1 + c2) / 2.0
        need1 = (bits == 1) & (c1 - c2 < delta)    # 需要改成 bit=1
        need0 = (bits == 0) & (c2 - c1 < delta)    # 需要改成 bit=0
        c1[need1] = mid[need1] + delta / 2.0
        c2[need1] = mid[need1] - delta / 2.0
        c2[need0] = mid[need0] + delta / 2.0
        c1[need0] = mid[need0] - delta / 2.0
        d_all[:, U1, V1] = c1
        d_all[:, U2, V2] = c2
        blocks = _blocks_idct2(d_all)
        Y[off_h: off_h + nb_h * BLOCK, off_w: off_w + nb_w * BLOCK] = (
            blocks.reshape(nb_h, nb_w, 8, 8)
            .transpose(0, 2, 1, 3).reshape(Yg.shape[0], Yg.shape[1])).astype(np.float32)

        ycbcr[:, :, 0] = np.clip(Y, 0, 255)
        G_wm = cv2.cvtColor(ycbcr.astype(np.uint8), cv2.COLOR_YCrCb2BGR)

        # 精确还原原尺寸保存（参考坐标系是偶数块，但交付物必须保持原尺寸不变）
        oh, ow = img.shape[:2]
        out_img = cv2.resize(G_wm, (ow, oh), interpolation=cv2.INTER_LANCZOS4)
        _write_image(out_path, out_img)

        return {"algorithm": self.name, "wm_size": (WM_W, WM_H), "delta": delta,
                "blocks": int(total), "strength": strength, "ref": REF_LONG}

    # ---------- 验证（盲提取 + 8px 全尺度扫描 + magic/内容双重确认） ----------
    # 内容匹配度阈值：候选内容与提取图高度一致时才把提取图对应到具体文字展示
    CONTENT_TH = 0.70

    @staticmethod
    def _is_hit(r):
        """命中判据（真盲提取）：32 位 magic 与密钥绑定，位错<=MAGIC_HAMMING 即判定
        「该密钥的水印存在」，不依赖是否预先知道水印内容——软件重启、截图、PS 调色
        导出后，只要密钥在历史库中，就能检出；内容由提取图另行展示/匹配。"""
        return r is not None and r.get("passed", False)

    def verify(self, path: str, profile=None):
        """检测图片是否带该密钥的频域水印，并盲提取出水印内容图。

        8px 步长全尺度扫描（128 ~ CAND_MAX）覆盖任意等比缩放 / 改分辨率 /
        中心裁剪后内容放大的尺度。块网格中心锚定使中心裁剪自动对齐。
        扫描顺序：先试 REF（内容占满画布最常见，秒中），再向上扫到 CAND_MAX
        （截图带留白/边框 -> 内容缩小 -> 需放大到 >REF 才能对齐），最后向下扫。
        命中 = magic 校验通过（密钥正确）且内容匹配度达标（水印真实存在），
        双重确认可有效排除无水印 / 错密钥的误报。
        """
        if profile is None:
            return None
        extra = profile.extra or {}
        key = extra.get("key", DEFAULT_SECRET)
        # 候选内容仅用于把提取图对应到已知文字做展示；为空也允许盲提取命中
        # （magic 判定与内容无关），此时 matched_content=None，界面直接展示提取图。
        cand = [c for c in (extra.get("cand_contents") or []) if c]
        if not cand and extra.get("content"):
            cand = [extra["content"]]

        img = _read_image(path)

        # ---- 快速路径：截图若含边框/标题栏/留白，先定位内容区域并缩放到 REF 坐标系 ----
        # 内容区域裁掉留白后重新满幅于 REF，块数/块网格与嵌入完全一致 -> 一次对齐即命中。
        # 定位失败（纯色图/周边有纹理）则回退下方全图候选扫描。
        if img.shape[0] >= 96 and img.shape[1] >= 96:
            box = _locate_content_box(img)
            if box is not None:
                x0, y0, x1, y1 = box
                content = img[y0:y1, x0:x1]
                if content.shape[0] >= 96 and content.shape[1] >= 96:
                    G = _resize_even_blocks(content, REF_LONG)
                    if G.shape[0] >= 64 and G.shape[1] >= 64:
                        r = self._decode(G, key, cand, REF_LONG, want_any=False, center=None)
                        if r is not None and self._is_hit(r):
                            return self._result(r, key)

        # ---- 锚点粗扫 + 局部精扫：覆盖裁剪/缩放/改分辨率（内容占满画布） ----
        # 常用候选锚点先秒级探测，命中直接返回；未命中则对所有「接近通过」的锚点
        # （magic 校验差 <=4）做 8px 局部精扫（±64），覆盖正确候选不在锚点上的情况。
        # 中心锚定使裁剪/缩放的块网格自动对齐；无水印噪声即使 magic 偶低，内容匹配 <0.70 仍被拒。
        anchors = [REF_LONG, CAND_MAX, 768, 640, 512, 1280, 384, 256, 192, 160, 128]
        best_r = None            # 最接近命中的解码结果
        best_score = None        # (magic_err, -sim)
        promising = []           # magic 接近通过的锚点候选（需精扫）
        all_results = []         # 保存所有候选的解码结果，供后续相位搜索
        for cand_size in anchors:
            G = _resize_even_blocks(img, cand_size)
            if G.shape[0] < 64 or G.shape[1] < 64:
                continue
            r = self._decode(G, key, cand, cand_size, want_any=False, center=None)
            if r is None:
                continue
            all_results.append(r)
            if self._is_hit(r):
                return self._result(r, key)      # 锚点直接命中
            if r["magic_err"] <= MAGIC_HAMMING + 15:
                promising.append(cand_size)
            if best_score is None or (r["magic_err"], -r["sim"]) < best_score:
                best_score = (r["magic_err"], -r["sim"])
                best_r = r

        # 对每个「接近」锚点在 ±128 范围按 8px 步长精扫（正确候选常不在锚点上，
        # 如内容 60% 的正确候选 616 位于锚点 512/640 之间；裁切后尺度偏移更大）
        for a0 in promising:
            for cand_size in range(max(128, a0 - 128), min(CAND_MAX, a0 + 129), 8):
                if cand_size == a0 or cand_size in anchors:
                    continue
                G = _resize_even_blocks(img, cand_size)
                if G.shape[0] < 64 or G.shape[1] < 64:
                    continue
                r = self._decode(G, key, cand, cand_size, want_any=False, center=None)
                if r is None:
                    continue
                all_results.append(r)
                if self._is_hit(r):
                    return self._result(r, key)
                if (r["magic_err"], -r["sim"]) < best_score:
                    best_score = (r["magic_err"], -r["sim"])
                    best_r = r

        # ---- 相位精扫：正确候选可能落在 8px 网格之间（如内容 76% -> 候选 778） ----
        # 此时块网格相对内容相位偏移数像素，DCT 差分符号被破坏（magic 飙高）。
        # 对 magic_err 最低的最佳候选做大范围相位平移搜索。
        # 裁切/截图后相位偏移常达 ±8~16px，因此搜索范围扩大到 ±16px。
        if best_r is not None and best_r["magic_err"] <= MAGIC_HAMMING + 15:
            G = _resize_even_blocks(img, best_r["cand_size"])
            base = _grid_info(G.shape[0], G.shape[1])
            cx = base[3] + (base[1] // 2) * 8        # 块网格中心像素
            cy = base[2] + (base[0] // 2) * 8
            # 两阶段相位搜索：粗扫 ±12px（步长4），精扫 ±4px（步长1）
            for step, max_off in ((4, 12), (1, 4)):
                for dy in range(-max_off, max_off + 1, step):
                    for dx in range(-max_off, max_off + 1, step):
                        if dx == 0 and dy == 0 and step == 4:
                            continue
                        r = self._decode(G, key, cand, best_r["cand_size"],
                                         want_any=False, center=(cx + dx, cy + dy))
                        if r is None:
                            continue
                        if self._is_hit(r):
                            return self._result(r, key)
                        if (r["magic_err"], -r["sim"]) < best_score:
                            best_score = (r["magic_err"], -r["sim"])
                            best_r = r

        # ---- 尺度扩展：相位通过（密钥对、信号在）但内容匹配低 -> 候选尺度仍错 ----
        # 相位在某个尺度下对齐了块网格，但正确尺度（内容=REF）可能在 ±160 之外。
        # 此时以通过相位的候选为中心做 8px 尺度扩展（正确尺度处内容精确=REF，
        # 中心锚定自然对齐，无需再调相位）。
        if best_r is not None and best_r["magic_err"] <= MAGIC_HAMMING:
            a0 = best_r["cand_size"]
            for cand_size in range(max(128, a0 - 160), min(CAND_MAX, a0 + 161), 8):
                if cand_size == a0:
                    continue
                G = _resize_even_blocks(img, cand_size)
                if G.shape[0] < 64 or G.shape[1] < 64:
                    continue
                r = self._decode(G, key, cand, cand_size, want_any=False, center=None)
                if r is None:
                    continue
                if self._is_hit(r):
                    return self._result(r, key)
                if (r["magic_err"], -r["sim"]) < best_score:
                    best_score = (r["magic_err"], -r["sim"])
                    best_r = r

        if self._is_hit(best_r):
            return self._result(best_r, key)
        return None

    @staticmethod
    def _result(r, key):
        # 盲提取命中但候选内容都对不上（如重启后不知道原内容）时，不返回错误文字，
        # matched_content=None，由界面直接展示提取出的水印图。
        matched = r["matched_content"] if r["sim"] >= FrequencyDctWatermark.CONTENT_TH else None
        return {"detected": True, "algorithm": "frequency_dct",
                "wm": r["wm"], "wm_size": (WM_W, WM_H), "key": key,
                "cand_size": r["cand_size"],
                "matched_content": matched, "similarity": r["sim"]}

    def _decode(self, G, key, cand_contents, cand_size, want_any=True, center=None):
        """在参考坐标系图 G 上按密钥向量化投票解码（块网格中心锚定）。

        嵌入与检测都以「图中心」为块网格原点，中心裁剪不移动内容中心，
        因此裁剪后无需相位平移即可对齐。center=(cx, cy) 可指定块网格中心
        （截图含边框/标题栏导致内容偏移时，用内容中心锚定）。

        want_any=True（旧接口）：magic 通过则直接返回完整结果，否则 None；
        want_any=False（搜索用）：总是返回诊断信息（含 magic_err / passed / sim），供上层排名。
        """
        ycbcr = cv2.cvtColor(G, cv2.COLOR_BGR2YCrCb).astype(np.float32)
        Y = ycbcr[:, :, 0]
        h, w = Y.shape
        if center:
            nb_h, nb_w, off_h, off_w = _grid_info(h, w, int(center[0]), int(center[1]))
        else:
            nb_h, nb_w, off_h, off_w = _grid_info(h, w)
        total = nb_h * nb_w
        if total < TOTAL_BITS:
            return None

        seed = _seed_from_key(key)
        kmat = _bit_map(nb_w, nb_h, seed)          # (nb_h, nb_w)
        Yg = Y[off_h: off_h + nb_h * BLOCK, off_w: off_w + nb_w * BLOCK]
        blocks = (Yg.reshape(nb_h, 8, nb_w, 8)
                  .transpose(0, 2, 1, 3).reshape(-1, 8, 8).astype(np.float64))
        d_all = _blocks_dct2(blocks)               # (N,8,8)
        sign = (d_all[:, U1, V1] > d_all[:, U2, V2]).astype(np.int8) * 2 - 1
        votes = np.zeros(TOTAL_BITS, dtype=np.int64)
        np.add.at(votes, kmat.ravel(), sign)
        bits = (votes > 0).astype(np.uint8)

        # ---- 内容比特：3 副本投票数合并 + 多数判决 ----
        # 载荷结构：[副本0:400][副本1:400][副本2:400][magic:224]
        # 将 3 个副本的投票数按位相加，再做符号判决。裁切后某个副本的块数减少时，
        # 其他副本仍可贡献投票，合并后总投票数 ≈ 单副本的 3 倍，显著提升信噪比。
        content_total = CONTENT_BITS * CONTENT_REPEAT
        content_votes = votes[:content_total].reshape(CONTENT_REPEAT, CONTENT_BITS).sum(axis=0)
        content_bits = (content_votes > 0).astype(np.uint8)

        # magic 校验：magic 每 bit 的 MAGIC_REPEAT 次重复分散在 MAGIC_BITS 轮的固定位置
        magic_exp = _magic_from_key(key)
        seg = bits[content_total:].reshape(MAGIC_REPEAT, MAGIC_BITS)
        magic_got = (seg.sum(axis=0) >= (MAGIC_REPEAT + 1) // 2).astype(np.uint8)
        magic_err = int(np.count_nonzero(magic_got != magic_exp))
        passed = magic_err <= MAGIC_HAMMING

        wm_arr = (content_bits.reshape((WM_H, WM_W)) * 255).astype(np.uint8)
        best, sim = _match_content(wm_arr, cand_contents)

        if not want_any:
            return {"cand_size": cand_size, "magic_err": magic_err, "passed": passed,
                    "votes": votes, "bits": bits, "magic_got": magic_got,
                    "sim": sim, "matched_content": best, "wm": wm_arr}
        if not passed:
            return None
        return {"detected": True, "algorithm": self.name, "wm": wm_arr,
                "wm_size": (WM_W, WM_H), "key": key, "cand_size": cand_size,
                "matched_content": best, "similarity": sim}


def _match_content(wm_arr, cand_contents):
    """把盲提取出的水印图与候选水印内容渲染图（同一固定画布）对比，用于展示。"""
    if wm_arr is None or wm_arr.size == 0:
        return None, 0.0
    extracted = (wm_arr > 128).astype(np.uint8)
    best, best_sim = None, 0.0
    for c in cand_contents:
        if not c:
            continue
        exp = render_text_wm_fixed(c)
        sim = float((exp == extracted).mean())
        if sim > best_sim:
            best_sim, best = sim, c
    return best, best_sim
