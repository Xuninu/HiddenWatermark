# -*- coding: utf-8 -*-
"""方案一：DCT 频域水印（周期瓦片导频同步 + 像素级亚块对齐 + magic 软相关选相）。

抗几何攻击能力（本版核心）：
  - 「周期瓦片导频」：把 8x8 DCT 块网格按 T x T（16x16 块 = 128x128px）划分周期瓦片，
    瓦片内 224 个块嵌入由密钥决定方向的「导频」，负责同步；其余 32 块承载数据
    （16bit 内容指纹 + 16bit magic 校验）。导频在整图周期性重复，截图保留任意一块
    瓦片区域都能重新锁定周期，因此**非对称、非等比、不从角落起裁的任意子区域截图**
    都能恢复同步。
  - 检测三层对齐：
      1) 粗搜瓦片块数尺度（x/y 独立，覆盖任意宽高比裁切后的独立缩放）；
      2) 像素级亚块对齐（搜索 DCT 块网格 0~7px 起点，补偿截图重采样造成的亚块平移）；
      3) FFT 一次算出 256 种瓦片相位，用 magic 归一化相关选最优相位。
  - 内容为 16bit 指纹（文本 CRC-16），配合密钥绑定的 16bit magic：同步 Z 与 magic
    双重确认，无水印 / 错密钥不会误报；本机历史内容库可把指纹回显成完整原文。

局限（所有频域水印共性）：
  - 极端裁切（保留画面 <25%）因完整瓦片过少，成功率下降；
  - 不抗旋转（需求明确不需要）；非等比「拉伸变形」通过 x/y 独立尺度搜索可覆盖常见范围；
  - 算法换代后旧版水印无法被本版识别，已加水印图片需重新添加。
"""
import hashlib
import os
import zlib

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .base import WatermarkAlgorithm

BLOCK = 8
# 选取的低频 DCT 系数对（(2,2) 与 (1,3)，实测两次 resize + JPEG75 后符号存活率≈1.0）
U1, V1 = 2, 2
U2, V2 = 1, 3
DEFAULT_SECRET = "hidden-watermark-default-key"
REF_LONG = 1024           # 参考坐标系长边：嵌入与提取共用

# ---- 周期瓦片导频参数（经 216 组裁切/压缩实验标定，同步 Z 完全分离、内容中位 100%）----
T = 16                    # 瓦片边长（块），16x16 块 = 128x128px
N = T * T                 # 每瓦片 256 块
N_PILOT = 224             # 导频块数（负责同步，越多同步 Z 越高）
N_DATA = N - N_PILOT      # 数据块 32
MAGIC_N = 16              # magic 校验位（密钥绑定，负责选相与防伪）
CONTENT_N = N_DATA - MAGIC_N   # 内容指纹 16bit
SYNC_Z_TH = 6.2           # 同步命中阈值（实测有水印 min Z=6.98 / 无水印 max Z=5.49）
MAGIC_ERR_TH = 3          # magic 允许位错（实测真实水印 max=1）
# 检测尺度搜索范围（参考坐标系块数）：覆盖竖图(高约128块)与横图
NB_MAX_H, NB_MAX_W = 130, 132
SCALE_NEIGH = 3           # 精搜尺度邻域 ±3 块

# 兼容旧引用（GUI / 实验脚本）
WM_W, WM_H = 40, 10
CONTENT_BITS = WM_W * WM_H

# 8x8 DCT-II 正交基（预计算，einsum 快速取单个系数）
_DCT_MAT = np.zeros((8, 8), dtype=np.float64)
for _i in range(8):
    for _j in range(8):
        _DCT_MAT[_i, _j] = (np.sqrt(1.0 / 8.0) if _i == 0
                            else np.sqrt(2.0 / 8.0) * np.cos(np.pi * (2 * _j + 1) * _i / 16.0))


# ============================ 图像 IO（兼容中文路径 / TGA / WebP）============================
def _read_image(path):
    """cv2.imdecode 按字节读入（兼容中文/空格路径）；OpenCV 不支持的格式回退 Pillow。"""
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        try:
            rgb = np.array(Image.open(path).convert("RGB"))
            img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        except Exception as e:
            raise ValueError(f"无法读取图片: {path}，错误: {e}")
    return img


def _write_image(path, img):
    """按原扩展名保存，尽量无损：PNG/TGA/BMP/TIFF 无损，JPEG q95，WebP 无损。"""
    ext = os.path.splitext(path)[1].lower() or ".png"
    if ext == ".jpg":
        ext = ".jpeg"
    if ext == ".tga":   # OpenCV 5.x 无 TGA 编码器，用 Pillow
        Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)).save(path, format="TGA")
        return
    params = []
    if ext in (".jpeg", ".jpg"):
        params = [cv2.IMWRITE_JPEG_QUALITY, 95]
    elif ext == ".webp":
        params = [cv2.IMWRITE_WEBP_LOSSLESS_MODE, cv2.IMWRITE_WEBP_LOSSLESS_ON]
    try:
        ok, buf = cv2.imencode(ext, img, params)
    except cv2.error:
        ok = False
    if not ok:
        try:
            Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)).save(path)
            return
        except Exception as e2:
            raise ValueError(f"保存失败（格式 {ext} 可能不支持）: {path}，错误: {e2}")
    buf.tofile(path)


def _resize_even_blocks(img, long_side):
    """等比缩放到参考坐标系（长边=long_side，保持宽高比）。"""
    h, w = img.shape[:2]
    scale = long_side / max(h, w)
    nw = max(8, int(round(w * scale)))
    nh = max(8, int(round(h * scale)))
    return cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LANCZOS4)


def _grid_info(h, w, cx=None, cy=None):
    """中心锚定块网格：返回 (nb_h, nb_w, off_h, off_w)，块网格以图中心为原点对称扩展。"""
    def _axis(L, c):
        c = int(c) if c is not None else int(L / 2.0 + 0.5)
        nb = min(c // BLOCK, (L - c) // BLOCK) * 2
        off = c - (nb // 2) * BLOCK
        return nb, off
    if cx is None:
        cx = int(w / 2.0 + 0.5)
    if cy is None:
        cy = int(h / 2.0 + 0.5)
    nb_h, off_h = _axis(h, cy)
    nb_w, off_w = _axis(w, cx)
    return nb_h, nb_w, off_h, off_w


def _seed_from_key(key: str) -> int:
    """密钥字符串 -> 稳定整数种子。"""
    return int.from_bytes(hashlib.sha256(f"hwm-dct::{key}".encode("utf-8")).digest()[:8], "big")


def _blocks_dct2(blocks):
    return np.einsum("ij,njk,lk->nil", _DCT_MAT, blocks, _DCT_MAT, optimize=True)


def _blocks_idct2(blocks):
    return np.einsum("ij,njk,lk->nil", _DCT_MAT.T, blocks, _DCT_MAT.T, optimize=True)


# ============================ 文本渲染（GUI 预览用）============================
def _pick_font(size: int):
    for fp in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
               "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc"):
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def render_text_wm(text):
    """把文本渲染成自适应尺寸二值图（1=文字, 0=底），用于 GUI 预览。"""
    text = (text or "").strip() or "HiddenWatermark"
    font = _pick_font(20)
    tmp = ImageDraw.Draw(Image.new("L", (8, 8)))
    bbox = tmp.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    img = Image.new("L", (tw + 12, th + 8), 255)
    ImageDraw.Draw(img).text((6 - bbox[0], 4 - bbox[1]), text, fill=0, font=font)
    return (np.array(img) < 128).astype(np.uint8)


def render_text_wm_fixed(text, w=WM_W, h=WM_H):
    """固定画布二值图（文字自适应字号），兼容旧引用。"""
    text = (text or "").strip() or "HiddenWatermark"
    size = 20
    font = _pick_font(size)
    tmp = ImageDraw.Draw(Image.new("L", (8, 8)))
    tw = tmp.textbbox((0, 0), text, font=font)[2]
    while tw > w - 2 and size > 6:
        size -= 1
        font = _pick_font(size)
        tw = ImageDraw.Draw(Image.new("L", (8, 8))).textbbox((0, 0), text, font=font)[2]
    bbox = ImageDraw.Draw(Image.new("L", (8, 8))).textbbox((0, 0), text, font=font)
    th = bbox[3] - bbox[1]
    img = Image.new("L", (w, h), 255)
    ImageDraw.Draw(img).text(((w - tw) // 2 - bbox[0], (h - th) // 2 - bbox[1]),
                             text, fill=0, font=font)
    return (np.array(img) < 128).astype(np.uint8)


# ============================ 内容指纹（文本 <-> 16bit）============================
def _content_to_bits(text: str) -> np.ndarray:
    """任意长度文本 -> 16bit 指纹（CRC-16）。本机历史库可凭指纹回显完整原文。"""
    data = (text or "").encode("utf-8", errors="ignore")
    fp = zlib.crc32(data) & 0xFFFF
    return np.array([(fp >> i) & 1 for i in range(CONTENT_N - 1, -1, -1)], dtype=np.uint8)


def _bits_to_fp(bits: np.ndarray) -> int:
    v = 0
    for b in bits:
        v = ((v << 1) | int(b)) & 0xFFFF
    return v


def _fp_match(fp: int, cand_contents):
    """把指纹与候选原文逐个比对，命中返回 (原文, 1.0)，否则 (None, 0.0)。"""
    for c in cand_contents or []:
        if c and _bits_to_fp(_content_to_bits(c)) == fp:
            return c, 1.0
    return None, 0.0


# ============================ 周期瓦片导频核心 ============================
def _layout(key):
    """由密钥确定性生成瓦片布局：导频/数据块位置、扩频方向 spn、magic 模式。"""
    rng = np.random.default_rng(_seed_from_key(key))
    perm = rng.permutation(N)
    pilot = np.sort(perm[:N_PILOT])
    data = np.sort(perm[N_PILOT:])
    spn = rng.choice([-1.0, 1.0], N)
    magic = rng.integers(0, 2, MAGIC_N).astype(np.uint8)
    return pilot, data, spn, magic


def _build_direction(content_bits, key):
    """生成 (T,T) 每块调制方向：导频块=spn，数据块=spn×载荷符号。"""
    _, data, spn, magic = _layout(key)
    payload = np.concatenate([content_bits.astype(np.uint8), magic])
    direction = spn.copy()
    for k, p in enumerate(data):
        direction[p] = spn[p] * (1.0 if payload[k] else -1.0)
    return direction.reshape(T, T)


def _build_template(key):
    """导频模板 (T,T)：导频位置=spn 方向，其余 0（同步相关用）。"""
    pilot, _, spn, _ = _layout(key)
    tmpl = np.zeros(N)
    tmpl[pilot] = spn[pilot]
    return tmpl.reshape(T, T)


def _dct_diff(Yg, nh, nw):
    """对 (nh*8, nw*8) 亮度块批量求两个低频系数之差，返回 (nh,nw)。"""
    b = Yg.reshape(nh, BLOCK, nw, BLOCK).transpose(0, 2, 1, 3).reshape(-1, BLOCK, BLOCK)
    a = np.einsum("i,kij,j->k", _DCT_MAT[U1], b, _DCT_MAT[V1])
    c = np.einsum("i,kij,j->k", _DCT_MAT[U2], b, _DCT_MAT[V2])
    return (a - c).reshape(nh, nw)


def _fold_z(diff, tmpl, band=2):
    """把块差分图按 T 周期折叠成瓦片并与导频模板做循环相关，返回同步相关面 (T,T)。
    只在导频位置去均值（消除 DC 偏置对 Z 的压低）。"""
    t = tmpl.shape[0]
    nh, nw = diff.shape
    mask = (tmpl != 0)
    acc = np.zeros((t, t))
    for r0 in range(0, nh - t + 1, t):
        for c0 in range(0, nw - t + 1, t):
            seg = diff[r0:min(r0 + band * t, nh), c0:min(c0 + band * t, nw)]
            kh, kw = seg.shape[0] // t, seg.shape[1] // t
            if kh < 1 or kw < 1:
                continue
            f = seg[:kh * t, :kw * t].reshape(kh, t, kw, t).mean(axis=(0, 2))
            fz = f * mask
            fz = fz - fz[mask > 0].mean() * mask
            tw = tmpl - tmpl[mask > 0].mean() * mask
            acc += np.real(np.fft.ifft2(np.fft.fft2(fz) * np.conj(np.fft.fft2(tw))))
    return acc


def _eval_offset(Yall, nh, nw, oy, ox, fc_conj, spn_s, data_pos, msign):
    """在给定尺度与一个像素偏移下，FFT 定相位并取 top3 相位归一化复核，
    返回该偏移的最优 (conf, bits)。"""
    ii = np.arange(nh)[:, None]
    jj = np.arange(nw)[None, :]
    diff = _dct_diff(Yall[oy:oy + nh * BLOCK, ox:ox + nw * BLOCK], nh, nw)
    f0 = np.zeros((T, T))
    np.add.at(f0, (ii % T, jj % T), diff)
    corr = np.real(np.fft.ifft2(np.fft.fft2(f0) * fc_conj))
    best = (-1e18, None)
    for pi in np.argpartition(corr.ravel(), -3)[-3:]:
        eph, epw = divmod(int(pi), T)
        aligned = np.roll(np.roll(f0, -eph, 0), -epw, 1).ravel() * spn_s
        v = aligned[data_pos]
        vm = v[CONTENT_N:]
        denom = np.sqrt((vm * vm).sum()) + 1e-9
        conf = float((vm * msign).sum() / denom)
        if conf > best[0]:
            best = (conf, (v > 0).astype(np.uint8))
    return best


def _decode_core(crop, key):
    """在任意裁切截图上盲解码。返回 dict(z, magic_err, content_bits, conf) 或 None（未命中）。"""
    _, data_pos, spn, magic_exp = _layout(key)
    tmpl = _build_template(key)
    ch, cw = crop.shape[:2]
    if ch < BLOCK * T // 2 or cw < BLOCK * T // 2:
        return None
    ar = cw / ch

    # ---- 1) 粗搜尺度（步长 2 块，块起点 0）：x/y 独立，覆盖非等比裁切 ----
    cands = []
    for nh in range(T, NB_MAX_H, 2):
        nw0 = round(nh * ar)
        for nw in range(max(T, nw0 - 2), min(NB_MAX_W, nw0 + 3)):
            Gr = cv2.resize(crop, (nw * BLOCK, nh * BLOCK), interpolation=cv2.INTER_LINEAR)
            Y = cv2.cvtColor(Gr, cv2.COLOR_BGR2YCrCb)[:, :, 0].astype(np.float64)
            diff = _dct_diff(Y, nh, nw)
            acc = _fold_z(diff, tmpl)
            flat = acc.ravel()
            im = flat.argmax()
            bg = np.delete(flat, im)
            z = (flat[im] - bg.mean()) / (bg.std() + 1e-9)
            cands.append((z, nh, nw))
    cands.sort(key=lambda x: -x[0])
    ztop, nh_star, nw_star = cands[0]
    if ztop < SYNC_Z_TH:
        return None     # 同步不达标 -> 判定无该密钥水印

    # ---- FFT 一次算出全部瓦片相位的 magic 循环相关 ----
    msign = (2 * magic_exp.astype(np.float64) - 1)
    magic_pos = data_pos[CONTENT_N:]
    C = np.zeros(N)
    C[magic_pos] = msign * np.sign(spn[magic_pos])
    fc_conj = np.conj(np.fft.fft2(C.reshape(T, T)))
    spn_s = np.sign(spn)

    # ---- 2) 精搜阶段一：峰尺度邻域 × 粗像素偏移 {0,2,4,6}，找每尺度最优 ----
    coarse = []   # (conf, nh, nw, oy, ox, bits)
    for nh in range(max(T, nh_star - SCALE_NEIGH), min(NB_MAX_H, nh_star + SCALE_NEIGH + 1)):
        for nw in range(max(T, nw_star - SCALE_NEIGH), min(NB_MAX_W, nw_star + SCALE_NEIGH + 1)):
            Gr = cv2.resize(crop, (nw * BLOCK + 7, nh * BLOCK + 7), interpolation=cv2.INTER_LINEAR)
            Yall = cv2.cvtColor(Gr, cv2.COLOR_BGR2YCrCb)[:, :, 0].astype(np.float64)
            scale_best = (-1e18, None, 0, 0)
            for oy in (0, 2, 4, 6):
                for ox in (0, 2, 4, 6):
                    conf, bits = _eval_offset(Yall, nh, nw, oy, ox, fc_conj, spn_s,
                                              data_pos, msign)
                    if conf > scale_best[0]:
                        scale_best = (conf, bits, oy, ox)
            coarse.append((scale_best[0], nh, nw, scale_best[2], scale_best[3], scale_best[1]))
    coarse.sort(key=lambda x: -x[0])

    # ---- 3) 精搜阶段二：top4 尺度在其最优粗偏移 ±1 邻域做全像素细化 ----
    best = (-1e18, None)
    for _conf0, nh, nw, oy0, ox0, _b in coarse[:4]:
        Gr = cv2.resize(crop, (nw * BLOCK + 7, nh * BLOCK + 7), interpolation=cv2.INTER_LINEAR)
        Yall = cv2.cvtColor(Gr, cv2.COLOR_BGR2YCrCb)[:, :, 0].astype(np.float64)
        for oy in range(max(0, oy0 - 1), min(BLOCK, oy0 + 2)):
            for ox in range(max(0, ox0 - 1), min(BLOCK, ox0 + 2)):
                conf, bits = _eval_offset(Yall, nh, nw, oy, ox, fc_conj, spn_s,
                                          data_pos, msign)
                if conf > best[0]:
                    best = (conf, bits)
    conf, bits = best
    if bits is None:
        return None
    content_bits = bits[:CONTENT_N]
    magic_got = bits[CONTENT_N:]
    magic_err = int(np.count_nonzero(magic_got != magic_exp))
    if magic_err > MAGIC_ERR_TH:
        return None
    return {"z": float(ztop), "magic_err": magic_err,
            "content_bits": content_bits, "conf": conf}


class FrequencyDctWatermark(WatermarkAlgorithm):
    """DCT 频域水印（周期瓦片导频同步，抗非对称/非等比截图裁切）。"""

    name = "frequency_dct"

    # ---------- 嵌入 ----------
    def embed(self, src_path: str, out_path: str, profile) -> object:
        extra = profile.extra or {}
        key = extra.get("key", DEFAULT_SECRET)
        strength = int(extra.get("strength", 40))
        delta = max(5.0, strength / 2.0)
        content_text = extra.get("content") or profile.owner or "HiddenWatermark"
        content_bits = _content_to_bits(content_text)

        img = _read_image(src_path)
        oh, ow = img.shape[:2]
        G = _resize_even_blocks(img, REF_LONG)
        yc = cv2.cvtColor(G, cv2.COLOR_BGR2YCrCb).astype(np.float32)
        Y = yc[:, :, 0]
        nh, nw, off_h, off_w = _grid_info(Y.shape[0], Y.shape[1])
        total = nh * nw
        if total < N * 2:
            raise ValueError(f"宿主图过小：参考坐标系仅 {total} 块，至少需要 {N*2} 块，请换更大图片。")

        direction = _build_direction(content_bits, key)
        rr, cc = np.meshgrid(np.arange(nh), np.arange(nw), indexing="ij")
        P = direction[rr % T, cc % T]

        Yg = Y[off_h:off_h + nh * BLOCK, off_w:off_w + nw * BLOCK]
        bl = (Yg.reshape(nh, BLOCK, nw, BLOCK)
              .transpose(0, 2, 1, 3).reshape(-1, BLOCK, BLOCK).astype(np.float64))
        d = _blocks_dct2(bl)
        c1 = d[:, U1, V1].copy()
        c2 = d[:, U2, V2].copy()
        mid = (c1 + c2) / 2.0
        p = P.ravel()
        d[:, U1, V1] = mid + p * delta / 2.0
        d[:, U2, V2] = mid - p * delta / 2.0
        bl = _blocks_idct2(d)
        Y[off_h:off_h + nh * BLOCK, off_w:off_w + nw * BLOCK] = (
            bl.reshape(nh, nw, BLOCK, BLOCK)
            .transpose(0, 2, 1, 3).reshape(nh * BLOCK, nw * BLOCK)).astype(np.float32)

        yc[:, :, 0] = np.clip(Y, 0, 255)
        G_wm = cv2.cvtColor(yc.astype(np.uint8), cv2.COLOR_YCrCb2BGR)
        out_img = cv2.resize(G_wm, (ow, oh), interpolation=cv2.INTER_LANCZOS4)  # 还原原尺寸
        _write_image(out_path, out_img)

        return {"algorithm": self.name, "delta": delta, "blocks": int(total),
                "strength": strength, "ref": REF_LONG,
                "fp": f"{_bits_to_fp(content_bits):04X}"}

    # ---------- 检测 ----------
    @staticmethod
    def _is_hit(r):
        return r is not None and r.get("detected", False)

    def verify(self, path: str, profile=None):
        """盲检测是否带该密钥水印；命中返回内容指纹/回显原文/提取预览图，否则 None。"""
        if profile is None:
            return None
        extra = profile.extra or {}
        key = extra.get("key", DEFAULT_SECRET)
        cand = [c for c in (extra.get("cand_contents") or []) if c]
        if not cand and extra.get("content"):
            cand = [extra["content"]]

        img = _read_image(path)
        dec = _decode_core(img, key)
        if dec is None:
            return None

        fp = _bits_to_fp(dec["content_bits"])
        matched, sim = _fp_match(fp, cand)
        label = matched or f"ID:{fp:04X}"
        wm_arr = (render_text_wm(label) * 255).astype(np.uint8)   # 0/255 灰度图供 GUI 预览
        return {"detected": True, "algorithm": "frequency_dct",
                "wm": wm_arr, "wm_size": wm_arr.shape[::-1], "key": key,
                "matched_content": matched, "similarity": sim,
                "fp": f"{fp:04X}", "sync_z": dec["z"],
                "magic_err": dec["magic_err"], "conf": dec["conf"]}
