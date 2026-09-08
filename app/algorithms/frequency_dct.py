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
MAGIC_REPEAT = 31         # magic 每 bit 重复嵌入次数（提取时多数判决，抗裁切/抗位错/抗 JPEG）
TOTAL_BITS = CONTENT_BITS * CONTENT_REPEAT + MAGIC_BITS * MAGIC_REPEAT
MAGIC_HAMMING = 10        # magic 允许的位错上限（32 位签名下：真实水印含缩放/JPEG/裁切
                          # 位错 0~10，无水印/错密钥位错 >=14，间隔清晰；偶合误报概率极低）
# 提取时的候选尺度（长边）：步长 64px，覆盖「原图等比缩放」与「中心裁剪后内容放大」的常见比例
CANDIDATES = [1024, 960, 896, 832, 768, 704, 640, 576, 512, 448, 384, 320, 256, 192, 128]

# ==================== 同步标记（Synchronization Pattern）====================
# 同步标记用于裁切/截图后的自对齐：在水印区域4个角+中心嵌入已知的强标记，
# 检测时先扫描找到标记位置，再根据标记位置反推块网格的尺度和相位偏移。
SYNC_U1, SYNC_V1 = U1, V1  # 同步标记用与数据相同的低频系数对(2,2)/(1,3)，耐两次resize
SYNC_U2, SYNC_V2 = U2, V2
SYNC_DELTA = 100.0          # 同步标记嵌入强度（远大于数据20，两次resize后仍≈98）
SYNC_MARKER_SIZE = 4        # 同步标记区域大小（4x4块，降低噪声标准差≈10，信噪比≈40σ）
SYNC_BITS = 32              # 同步标记的比特数（32bit 使误匹配概率 <0.01%）
SYNC_MATCH_TH = 26          # 同步标记匹配阈值（32bit 中至少匹配26bit才算找到标记）
SYNC_CORNER_OFF = 2         # 同步标记距块网格角的偏移（块数），避免太靠边被裁掉

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


def _sync_pattern_from_key(key: str) -> np.ndarray:
    """密钥 -> 32 bit 同步标记模式（确定性，错密钥无法匹配）。
    使用与 magic 不同的派生前缀，确保同步标记和 magic 是独立的伪随机序列。"""
    d = hashlib.sha256(f"hwm-sync::{key}".encode("utf-8")).digest()
    return np.array([(d[i // 8] >> (i % 8)) & 1 for i in range(SYNC_BITS)], dtype=np.uint8)


def _sync_corner_blocks(nb_h, nb_w):
    """返回4个角的同步标记块坐标 (row, col)：左上、右上、左下、右下。
    返回的是 SYNC_MARKER_SIZE x SYNC_MARKER_SIZE 区域的左上角坐标。
    距角偏移 SYNC_CORNER_OFF 块，避免太靠边被裁掉。"""
    MS = SYNC_MARKER_SIZE
    r0, r1 = SYNC_CORNER_OFF, nb_h - SYNC_CORNER_OFF - MS
    c0, c1 = SYNC_CORNER_OFF, nb_w - SYNC_CORNER_OFF - MS
    return [(r0, c0), (r0, c1), (r1, c0), (r1, c1)]


def _scan_sync_markers(Y, key):
    """扫描同步标记：在 Y 通道中找 SYNC 系数对差分最大的标记区域，
    根据标记位置反推块网格的左上角和块数。

    支持4个/3个/2个标记定位：
    - 4个标记构成矩形：最可靠，优先级最高
    - 3个标记构成L形：裁掉1个角后可用
    - 2个标记：尝试所有6种角组合，裁掉2个角后可用

    相位搜索：块网格可能从像素(ph,pw)开始（0<=ph,pw<8），先在相位(0,0)下
    找候选区域，再对每个候选区域尝试8x8种相位做局部DCT，找到差分最大的相位，
    最后用最佳相位做全图DCT定位所有标记。

    返回 (off_h, off_w, nb_h, nb_w) 或 None（未找到有效标记）。
    """
    h, w = Y.shape
    if h < 64 or w < 64:
        return None

    def _region_diff_at_phase(ph, pw):
        """在指定相位(ph,pw)下做全图DCT，返回4x4区域平均差分图和块网格大小。"""
        nb_hf = (h - ph) // BLOCK
        nb_wf = (w - pw) // BLOCK
        MS = SYNC_MARKER_SIZE  # 4
        if nb_hf < MS + 1 or nb_wf < MS + 1:
            return None, 0, 0
        Yg = Y[ph:ph + nb_hf * BLOCK, pw:pw + nb_wf * BLOCK]
        blocks = (Yg.reshape(nb_hf, 8, nb_wf, 8)
                  .transpose(0, 2, 1, 3).reshape(-1, 8, 8).astype(np.float64))
        d = _blocks_dct2(blocks)
        sd = (d[:, SYNC_U1, SYNC_V1] - d[:, SYNC_U2, SYNC_V2]).reshape(nb_hf, nb_wf)
        # 用积分图快速计算4x4区域平均差分
        integral = np.cumsum(np.cumsum(sd, axis=0), axis=1)
        rd = np.zeros((nb_hf - MS + 1, nb_wf - MS + 1), dtype=np.float64)
        for r in range(nb_hf - MS + 1):
            for c in range(nb_wf - MS + 1):
                r2, c2 = r + MS - 1, c + MS - 1
                s = integral[r2, c2]
                if r > 0: s -= integral[r - 1, c2]
                if c > 0: s -= integral[r2, c - 1]
                if r > 0 and c > 0: s += integral[r - 1, c - 1]
                rd[r, c] = s / (MS * MS)
        return rd, nb_hf, nb_wf

    # 1. 8x8相位全图搜索：对每种相位做全图DCT，找全图中4x4区域平均差分的最大值，
    # 找到差分最大的相位。不假设标记在块网格的4个角，直接找全图最大差分区域，
    # 这样非对称裁切后即使只有1个标记保留也能找到正确相位。
    # 64次全图DCT约需2-3秒，确保找到正确相位。
    MS = SYNC_MARKER_SIZE
    best_phase = (0, 0)
    best_phase_score = -1e9
    best_region_diff = None
    best_nb_hf = best_nb_wf = 0
    for ph in range(8):
        for pw in range(8):
            rd, nb_hf, nb_wf = _region_diff_at_phase(ph, pw)
            if rd is None:
                continue
            # 直接用全图最大差分作为相位判据（不假设标记位置）
            score = float(rd.max())
            if score > best_phase_score:
                best_phase_score = score
                best_phase = (ph, pw)
                best_region_diff = rd
                best_nb_hf, best_nb_wf = nb_hf, nb_wf

    if best_region_diff is None or best_phase_score < SYNC_DELTA * 0.4:
        return None

    # 2. 用最佳相位做全图DCT，找到所有同步标记的位置
    ph, pw = best_phase
    region_diff = best_region_diff
    nb_h_full, nb_w_full = best_nb_hf, best_nb_wf

    # 取差分最大的 15 个候选区域
    flat_idx = np.argsort(region_diff.ravel())[::-1][:15]
    candidates = []
    for idx in flat_idx:
        r, c = np.unravel_index(idx, region_diff.shape)
        candidates.append((r, c, float(region_diff[r, c])))

    if not candidates or candidates[0][2] < SYNC_DELTA * 0.3:
        return None

    corner_set = {(c[0], c[1]): c[2] for c in candidates}

    # 标记在块网格中的位置（4x4区域左上角的块坐标）：
    # TL=(2,2), TR=(2, nb_w-6), BL=(nb_h-6, 2), BR=(nb_h-6, nb_w-6)
    # CENTER=(nb_h//2-2, nb_w//2-2) —— 中心标记，与任意角标记配合即可定位
    # 标记间距：BR-TL = (nb_h-8, nb_w-8)
    OFF = SYNC_CORNER_OFF  # =2
    MARKER_W = SYNC_MARKER_SIZE  # 4x4区域

    def _validate(off_h, off_w, nb_h, nb_w):
        """验证块网格是否大小合理，且至少有一部分在图像范围内。"""
        if nb_h < 8 or nb_w < 8 or nb_h > 256 or nb_w > 256:
            return False
        # 允许负偏移：块网格左上角可能在图像外（非对称裁切）
        # 但至少要有一部分在图像内
        if off_h >= h or off_w >= w:
            return False
        if off_h + nb_h * BLOCK <= 0 or off_w + nb_w * BLOCK <= 0:
            return False
        return True

    best_result = None
    best_total = 0

    # ========== 单标记反推（优先级最高，非对称裁切后只有1个标记保留时使用） ==========
    # 不假设标记在当前块网格的4个角，根据标记在原始块网格中的已知位置反推。
    # 原始块网格大小由当前图像尺寸决定（假设裁切后宽高比不变）。
    ref_nb_h = h // BLOCK
    ref_nb_w = w // BLOCK
    marker_positions = {
        'TL': (2, 2),
        'TR': (2, ref_nb_w - 6),
        'BL': (ref_nb_h - 6, 2),
        'BR': (ref_nb_h - 6, ref_nb_w - 6),
        'CENTER': (ref_nb_h // 2 - 2, ref_nb_w // 2 - 2),
    }
    for (r, c, s) in candidates[:10]:
        if s < SYNC_DELTA * 0.3:
            continue
        for mname, (mr, mc) in marker_positions.items():
            grid_r = r - mr
            grid_c = c - mc
            off_h_est = grid_r * BLOCK
            off_w_est = grid_c * BLOCK
            if (ref_nb_h < 8 or ref_nb_w < 8 or
                    off_h_est + ref_nb_h * BLOCK < 0 or
                    off_w_est + ref_nb_w * BLOCK < 0 or
                    off_h_est > h or off_w_est > w):
                continue
            vis_h = min(h, off_h_est + ref_nb_h * BLOCK) - max(0, off_h_est)
            vis_w = min(w, off_w_est + ref_nb_w * BLOCK) - max(0, off_w_est)
            if vis_h < BLOCK * 8 or vis_w < BLOCK * 8:
                continue
            if s > best_total:
                best_total = s
                best_result = (off_h_est, off_w_est, ref_nb_h, ref_nb_w)

    # ========== 4个标记构成矩形（最可靠，单标记失败时使用） ==========
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            r1, c1, s1 = candidates[i]
            r2, c2, s2 = candidates[j]
            if r1 == r2 or c1 == c2:
                continue
            if (r1, c2) in corner_set and (r2, c1) in corner_set:
                s3 = corner_set[(r1, c2)]
                s4 = corner_set[(r2, c1)]
                total = s1 + s2 + s3 + s4
                # 要求4个标记差分都足够大，且总差分 > SYNC_DELTA*2，避免噪声误判
                if min(s1, s2, s3, s4) < SYNC_DELTA * 0.3 or total < SYNC_DELTA * 2:
                    continue
                mr0, mc0 = min(r1, r2), min(c1, c2)
                mr1, mc1 = max(r1, r2), max(c1, c2)
                # TL=(2,2), BR=(nb_h-6, nb_w-6), 间距=(nb_h-8, nb_w-8)
                nb_h = (mr1 - mr0) + 8
                nb_w = (mc1 - mc0) + 8
                off_h = (mr0 - OFF) * BLOCK
                off_w = (mc0 - OFF) * BLOCK
                if _validate(off_h, off_w, nb_h, nb_w) and total > best_total:
                    best_total = total
                    best_result = (off_h, off_w, nb_h, nb_w)

    # ========== 3个标记构成L形 ==========
    if best_result is None:
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                for k in range(j + 1, len(candidates)):
                    r1, c1, s1 = candidates[i]
                    r2, c2, s2 = candidates[j]
                    r3, c3, s3 = candidates[k]
                    rows = {r1, r2, r3}
                    cols = {c1, c2, c3}
                    # L形：恰好2个不同行和2个不同列
                    if len(rows) == 2 and len(cols) == 2:
                        total = s1 + s2 + s3
                        mr0, mc0 = min(rows), min(cols)
                        mr1, mc1 = max(rows), max(cols)
                        nb_h = (mr1 - mr0) + 8
                        nb_w = (mc1 - mc0) + 8
                        off_h = (mr0 - OFF) * BLOCK
                        off_w = (mc0 - OFF) * BLOCK
                        if _validate(off_h, off_w, nb_h, nb_w) and total > best_total:
                            best_total = total
                            best_result = (off_h, off_w, nb_h, nb_w)

    # ========== 2个标记（尝试角组合 + 中心+角组合） ==========
    if best_result is None:
        est_nb_h = min(nb_h_full, 128)
        est_nb_w = min(nb_w_full, 128)
        top_candidates = candidates[:10]

        for i in range(len(top_candidates)):
            for j in range(i + 1, len(top_candidates)):
                r1, c1, s1 = top_candidates[i]
                r2, c2, s2 = top_candidates[j]
                dr = r2 - r1
                dc = c2 - c1
                total = s1 + s2
                if total < SYNC_DELTA * 0.6:
                    continue

                combos = []
                # 标记位置(4x4左上角)：TL=(2,2), TR=(2,nb_w-6), BL=(nb_h-6,2), BR=(nb_h-6,nb_w-6)
                # 角间距：TL-TR=(0,nb_w-8), TL-BL=(nb_h-8,0), TL-BR=(nb_h-8,nb_w-8)
                #         TR-BL=(nb_h-8,-(nb_w-8)), TR-BR=(nb_h-8,0), BL-BR=(0,nb_w-8)

                # TL-TR: 同行
                if dr == 0 and dc > 0:
                    combos.append((est_nb_h, dc + 8, 2, 2))
                # TL-BL: 同列
                if dc == 0 and dr > 0:
                    combos.append((dr + 8, est_nb_w, 2, 2))
                # TL-BR
                if dr > 0 and dc > 0:
                    combos.append((dr + 8, dc + 8, 2, 2))
                # TR-BL: 标记1=TR=(2,nb_w-6)
                if dr > 0 and dc < 0:
                    nb_w = -dc + 8
                    combos.append((dr + 8, nb_w, 2, nb_w - 6))
                # TR-BR: 标记1=TR=(2,nb_w-6), 同列
                if dc == 0 and dr > 0:
                    combos.append((dr + 8, est_nb_w, 2, est_nb_w - 6))
                # BL-BR: 标记1=BL=(nb_h-6,2), 同行
                if dr == 0 and dc > 0:
                    combos.append((est_nb_h, dc + 8, est_nb_h - 6, 2))

                # 中心+角组合（标记1=中心=(nb_h//2-2,nb_w//2-2)）
                # 中心→TL: dr=-(nb_h//2-4), dc=-(nb_w//2-4)
                if dr < -1 and dc < -1:
                    nb_h = 2 * (-dr + 4)
                    nb_w = 2 * (-dc + 4)
                    combos.append((nb_h, nb_w, nb_h // 2 - 2, nb_w // 2 - 2))
                # 中心→TR: dr=-(nb_h//2-4), dc=nb_w//2-4
                if dr < -1 and dc > 1:
                    nb_h = 2 * (-dr + 4)
                    nb_w = 2 * (dc + 4)
                    combos.append((nb_h, nb_w, nb_h // 2 - 2, nb_w // 2 - 2))
                # 中心→BL: dr=nb_h//2-4, dc=-(nb_w//2-4)
                if dr > 1 and dc < -1:
                    nb_h = 2 * (dr + 4)
                    nb_w = 2 * (-dc + 4)
                    combos.append((nb_h, nb_w, nb_h // 2 - 2, nb_w // 2 - 2))
                # 中心→BR: dr=nb_h//2-4, dc=nb_w//2-4
                if dr > 1 and dc > 1:
                    nb_h = 2 * (dr + 4)
                    nb_w = 2 * (dc + 4)
                    combos.append((nb_h, nb_w, nb_h // 2 - 2, nb_w // 2 - 2))

                # 角+中心组合（标记1=角，标记2=中心）
                # TL→中心: dr=nb_h//2-4, dc=nb_w//2-4
                if dr > 1 and dc > 1:
                    combos.append((2 * (dr + 4), 2 * (dc + 4), 2, 2))
                # TR→中心: dr=nb_h//2-4, dc=-(nb_w//2-4)
                if dr > 1 and dc < -1:
                    nb_w = 2 * (-dc + 4)
                    combos.append((2 * (dr + 4), nb_w, 2, nb_w - 6))
                # BL→中心: dr=-(nb_h//2-4), dc=nb_w//2-4
                if dr < -1 and dc > 1:
                    nb_h = 2 * (-dr + 4)
                    combos.append((nb_h, 2 * (dc + 4), nb_h - 6, 2))
                # BR→中心: dr=-(nb_h//2-4), dc=-(nb_w//2-4)
                if dr < -1 and dc < -1:
                    nb_h = 2 * (-dr + 4)
                    nb_w = 2 * (-dc + 4)
                    combos.append((nb_h, nb_w, nb_h - 6, nb_w - 6))

                for nb_h, nb_w, gr1, gc1 in combos:
                    off_h = (r1 - gr1) * BLOCK
                    off_w = (c1 - gc1) * BLOCK
                    if _validate(off_h, off_w, nb_h, nb_w) and total > best_total:
                        best_total = total
                        best_result = (off_h, off_w, nb_h, nb_w)

    if best_result is None or best_total < SYNC_DELTA * 0.3:
        return None

    off_h, off_w, nb_h, nb_w = best_result
    # 加上相位偏移（块网格实际从像素(ph,pw)开始）
    off_h += ph
    off_w += pw
    # 不钳制到图像范围内——非对称裁切后块网格左上角可能在图像外
    return off_h, off_w, nb_h, nb_w


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


def _bit_map(nb_w, nb_h, seed, ref_nb_h=None, ref_nb_w=None, off_r=0, off_c=0):
    """所有块 -> 载荷位的映射矩阵 (nb_h, nb_w)：
    基于「块相对参考坐标系中心的坐标 + 密钥种子」的乘法散列。

    默认 ref_nb_h/ref_nb_w=None 时用当前块网格大小作参考（中心锚定模式，
    中心裁切后相对坐标不变 -> 同一物理块映射到同一载荷位）。

    指定 ref_nb_h/ref_nb_w 和 off_r/off_c 时，用固定参考坐标系（非对称裁切模式）：
    当前块(i,j)对应参考坐标系块(i+off_r, j+off_c)，再相对参考中心计算映射。
    这样非对称裁切后只要找到正确的块偏移，映射关系就和嵌入时完全一致。

    返回 numpy int64 矩阵，与总块数无关，向量化快速计算。"""
    if ref_nb_h is None:
        ref_nb_h = nb_h
    if ref_nb_w is None:
        ref_nb_w = nb_w
    rx = np.arange(nb_w, dtype=np.int64) + off_c - ref_nb_w // 2
    ry = np.arange(nb_h, dtype=np.int64) + off_r - ref_nb_h // 2
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

        # ---- 同步标记嵌入：在4个角 + 中心各嵌入一个 4x4 的强标记区域 ----
        # 使用与数据相同的低频系数对(2,2)/(1,3)，强嵌入 bit=1（c1-c2 >= SYNC_DELTA）。
        # 4x4区域降低噪声标准差（≈10），强度100远大于数据20，两次resize后差分仍≈98。
        # 4个角标记用于完整图的自定位；中心标记用于非对称裁切（角标记被裁掉后，
        # 中心标记+任意一个剩余角标记仍可定位块网格）。
        sync_pattern = _sync_pattern_from_key(key)  # 32bit 伪随机模式（预留，当前用全1强标记）
        corners = _sync_corner_blocks(nb_h, nb_w)
        # 中心标记位置：4x4区域的左上角，使区域中心对齐块网格中心
        center_pos = (nb_h // 2 - SYNC_MARKER_SIZE // 2, nb_w // 2 - SYNC_MARKER_SIZE // 2)
        all_markers = corners + [center_pos]
        for (cr, cc) in all_markers:
            for dr in range(SYNC_MARKER_SIZE):
                for dc in range(SYNC_MARKER_SIZE):
                    br, bc = cr + dr, cc + dc
                    if 0 <= br < nb_h and 0 <= bc < nb_w:
                        idx = br * nb_w + bc
                        sc1 = d_all[idx, SYNC_U1, SYNC_V1]
                        sc2 = d_all[idx, SYNC_U2, SYNC_V2]
                        smid = (sc1 + sc2) / 2.0
                        # 强嵌入 bit=1：让 sc1 - sc2 >= SYNC_DELTA（覆盖数据嵌入）
                        if sc1 - sc2 < SYNC_DELTA:
                            d_all[idx, SYNC_U1, SYNC_V1] = smid + SYNC_DELTA / 2.0
                            d_all[idx, SYNC_U2, SYNC_V2] = smid - SYNC_DELTA / 2.0

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

        # ---- 同步标记定位路径：裁切/截图后用4个角的同步标记自对齐 ----
        # 在几个主要候选尺度下扫描同步标记，如果找到4个构成矩形的标记，
        # 就根据标记位置反推块网格的偏移和大小，直接对齐提取。
        # 这能解决中心裁切75%和非对称裁切后的尺度/相位对齐问题。
        sync_candidates = [REF_LONG, 768, 512, 1280, 640, 896, 960]
        for sync_size in sync_candidates:
            G_sync = _resize_even_blocks(img, sync_size)
            if G_sync.shape[0] < 64 or G_sync.shape[1] < 64:
                continue
            ycbcr_sync = cv2.cvtColor(G_sync, cv2.COLOR_BGR2YCrCb).astype(np.float32)
            Y_sync = ycbcr_sync[:, :, 0]
            grid = _scan_sync_markers(Y_sync, key)
            if grid is not None:
                off_h, off_w, nb_h, nb_w = grid
                if nb_h * nb_w >= TOTAL_BITS:
                    r = self._decode(G_sync, key, cand, sync_size, want_any=False,
                                      grid_info=(nb_h, nb_w, off_h, off_w))
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

        # ---- 兜底：全图块网格偏移搜索（非对称裁切/截图带边框时，中心锚定失效） ----
        # 对几个主要候选尺度，使用当前图像块网格大小作参考坐标系，尝试不同偏移（含负偏移），
        # 找 magic_err 最小的组合。非对称裁切掉左/上边后，块网格左上角偏移到图像外（负偏移），
        # 中心锚定找不到，但全图搜索能遍历到正确的偏移位置。
        # _decode 已支持负偏移和参考坐标系映射，因此裁切后映射关系与嵌入时完全一致。
        fallback_sizes = [REF_LONG, 896, 768, 720, 512]
        for fsize in fallback_sizes:
            Gf = _resize_even_blocks(img, fsize)
            if Gf.shape[0] < 64 or Gf.shape[1] < 64:
                continue
            hf, wf = Gf.shape[:2]
            # 参考坐标系大小 = 中心锚定的块网格大小（与嵌入时一致）
            ref_nb_h, ref_nb_w, _, _ = _grid_info(hf, wf)
            if ref_nb_h * ref_nb_w < TOTAL_BITS:
                continue
            fbest_magic = 999
            fbest_r = None
            # 粗扫：步长16，覆盖主要偏移位置
            off_h_range = range(-ref_nb_h * BLOCK // 2, hf, 16)
            off_w_range = range(-ref_nb_w * BLOCK // 2, wf, 16)
            for off_h in off_h_range:
                for off_w in off_w_range:
                    r = self._decode(Gf, key, cand, fsize, want_any=False,
                                      grid_info=(ref_nb_h, ref_nb_w, off_h, off_w))
                    if r is not None and r["magic_err"] < fbest_magic:
                        fbest_magic = r["magic_err"]
                        fbest_r = r
                        if fbest_magic <= MAGIC_HAMMING:
                            break
                if fbest_magic <= MAGIC_HAMMING:
                    break
            if fbest_r is not None and self._is_hit(fbest_r):
                return self._result(fbest_r, key)

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

    def _decode(self, G, key, cand_contents, cand_size, want_any=True, center=None, grid_info=None):
        """在参考坐标系图 G 上按密钥向量化投票解码（块网格中心锚定）。

        嵌入与检测都以「图中心」为块网格原点，中心裁剪不移动内容中心，
        因此裁剪后无需相位平移即可对齐。center=(cx, cy) 可指定块网格中心
        （截图含边框/标题栏导致内容偏移时，用内容中心锚定）。

        grid_info=(nb_h, nb_w, off_h, off_w) 可直接指定块网格（同步标记定位后使用），
        指定时忽略 center 参数，直接用给定的块网格位置和大小提取。

        want_any=True（旧接口）：magic 通过则直接返回完整结果，否则 None；
        want_any=False（搜索用）：总是返回诊断信息（含 magic_err / passed / sim），供上层排名。
        """
        ycbcr = cv2.cvtColor(G, cv2.COLOR_BGR2YCrCb).astype(np.float32)
        Y = ycbcr[:, :, 0]
        h, w = Y.shape
        use_ref_map = False  # 是否使用参考坐标系映射（非对称裁切/负偏移时）
        if grid_info is not None:
            nb_h, nb_w, off_h, off_w = grid_info
            # 允许负偏移：块网格左上角可能在图像外（非对称裁切）
            # 计算可见像素范围
            y0 = max(0, off_h)
            y1 = min(h, off_h + nb_h * BLOCK)
            x0 = max(0, off_w)
            x1 = min(w, off_w + nb_w * BLOCK)
            if y1 - y0 < BLOCK * 4 or x1 - x0 < BLOCK * 4:
                return None  # 可见区域太小
            # 可见块在块网格中的起始位置
            start_r = (y0 - off_h) // BLOCK
            start_c = (x0 - off_w) // BLOCK
            vis_nb_h = (y1 - y0) // BLOCK
            vis_nb_w = (x1 - x0) // BLOCK
            if vis_nb_h * vis_nb_w < TOTAL_BITS // 2:
                return None
            # 如果有负偏移，使用参考坐标系映射
            if off_h < 0 or off_w < 0:
                use_ref_map = True
                ref_nb_h = h // BLOCK
                ref_nb_w = w // BLOCK
                map_off_r = off_h // BLOCK + start_r
                map_off_c = off_w // BLOCK + start_c
            # 用可见部分替换块网格参数
            nb_h, nb_w = vis_nb_h, vis_nb_w
            off_h, off_w = y0, x0
        elif center:
            nb_h, nb_w, off_h, off_w = _grid_info(h, w, int(center[0]), int(center[1]))
        else:
            nb_h, nb_w, off_h, off_w = _grid_info(h, w)
        total = nb_h * nb_w
        if total < TOTAL_BITS // 2:
            return None

        seed = _seed_from_key(key)
        if use_ref_map:
            kmat = _bit_map(nb_w, nb_h, seed, ref_nb_h, ref_nb_w, map_off_r, map_off_c)
        else:
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
                    "sim": sim, "matched_content": best, "wm": wm_arr,
                    "grid_off_h": off_h, "grid_off_w": off_w}
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
