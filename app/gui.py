# -*- coding: utf-8 -*-
"""图形界面（XnConvert 风格布局）。

布局参考 XnConvert：
  - 左侧：文件列表（支持拖拽图片/文件夹直接加入）
  - 右侧：所选图片的大图预览
  - 中部：动作板块「添加隐藏水印」——标记水印 / 频域水印可分别勾选、可叠加
  - 底部：输出设置 + 运行日志 + 状态栏

启动：python gui.py（加 --smoke 可自检后自动关闭）
"""
import os
import queue
import sys
import json
import tempfile
import math
import threading
from datetime import datetime
import tkinter as tk
from tkinter import messagebox
from tkinter import filedialog, ttk

from PIL import Image, ImageTk

import numpy as np

from .batch import apply_profiles
from .config import IMAGE_EXTS
from .engine import WatermarkEngine
from .models import WatermarkProfile
from .algorithms.frequency_dct import DEFAULT_SECRET

try:
    import windnd
    HAS_DND = True
    DND_IMPORT_ERR = None
except Exception as _e:  # noqa: BLE001
    HAS_DND = False
    DND_IMPORT_ERR = repr(_e)

# 用普通 tk.Tk（拖拽改用 windnd 的 WM_DROPFILES，不依赖 tkdnd/OLE）
ROOT_CLS = tk.Tk

# ---- 配色（简约现代：浅灰蓝底 + 白色卡片 + 主蓝，参考 hiddenwatermark_ui.html）----
BG = "#F1F5F9"            # slate-100 页面背景
CARD = "#FFFFFF"
ACCENT = "#2563EB"        # primary-600 主蓝
ACCENT_HOVER = "#1D4ED8"  # primary-700
ACCENT_LIGHT = "#EFF6FF"  # primary-50 浅蓝（主按钮弱化态/高亮底）
ACCENT_LIGHT2 = "#DBEAFE" # primary-100
TEXT = "#1E293B"          # slate-800 主文字
MUTED = "#64748B"         # slate-500 次要文字
BORDER = "#E2E8F0"        # slate-200 卡片边框
SLATE50 = "#F8FAFC"       # 标题栏浅灰底
BTN_GRAY = "#F1F5F9"      # 次级按钮底
LOG_BG = "#0F172A"        # slate-900 深色日志终端
LOG_FG = "#CBD5E1"        # slate-300 日志正文
HEADER_BG = "#FFFFFF"     # 顶栏白底
MONO = ("Consolas", 10)
UI = ("Microsoft YaHei UI", 10)
UI_BOLD = ("Microsoft YaHei UI", 11, "bold")

# 频域检测缓存状态哨兵：区分“检测中”和“已检测但未命中(None)”
_WM_LOADING = object()

# ---- 左侧图标阵列（网格缩略图）参数 ----
GRID_CELL_W = 118      # 每个单元格宽
GRID_CELL_H = 156      # 每个单元格高
GRID_THUMB = 96        # 缩略图边长
GRID_TEXT_H = 42       # 文件名文字区高度


def _collect_images(path):
    """返回 path 下所有图片（文件则单张，文件夹则递归）。"""
    if os.path.isfile(path):
        return [path] if os.path.splitext(path)[1].lower() in IMAGE_EXTS else []
    out = []
    for root, _dirs, files in os.walk(path):
        for fn in sorted(files):
            if os.path.splitext(fn)[1].lower() in IMAGE_EXTS:
                out.append(os.path.join(root, fn))
    return out


def _keys_path() -> str:
    """密钥历史文件位置（与 profiles.json 同目录）：用于验证时自动尝试已用过的密钥。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "HiddenWatermark")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "keys.json")


def _load_known_keys() -> set:
    try:
        with open(_keys_path(), "r", encoding="utf-8") as f:
            return {k for k in json.load(f).get("keys", []) if isinstance(k, str) and k}
    except Exception:  # noqa: BLE001
        return set()


def _save_known_keys(keys: set):
    try:
        with open(_keys_path(), "w", encoding="utf-8") as f:
            json.dump({"keys": sorted(keys)}, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def _contents_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "HiddenWatermark")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "contents.json")


def _load_known_contents() -> set:
    """加载历史用过的频域水印内容，重启后检测自己加过的水印可直接匹配出文字。"""
    try:
        with open(_contents_path(), "r", encoding="utf-8") as f:
            return {c for c in json.load(f).get("contents", []) if isinstance(c, str) and c}
    except Exception:  # noqa: BLE001
        return set()


def _save_known_contents(contents: set):
    try:
        with open(_contents_path(), "w", encoding="utf-8") as f:
            json.dump({"contents": sorted(contents)}, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass




def _profile_desc(p) -> str:
    """把水印档位转成一行可读描述（用于加水印结果）。"""
    extra = p.extra or {}
    if p.algorithm == "frequency_dct":
        return f"频域水印（密钥={extra.get('key', '?')}，内容={extra.get('content') or '无'}，强度={extra.get('strength', '?')}）"
    if p.algorithm == "data_append":
        return f"标记水印（归属人={p.owner}，水印内容={p.note or '无'}）"
    return p.name


# ================= 高斯模糊壁纸 + 圆角玻璃卡片 =================
_WALLPAPER_PATH = os.path.join(tempfile.gettempdir(), "hw_wallpaper.png")



def _rounded_pts(w, h, r, n=12):
    """生成精确四分之一圆弧的圆角矩形多边形点（扁平 [x1,y1,x2,y2,...]）。
    四个角都是半径 r 的 90° 标准圆弧，全等。"""
    r = min(r, w / 2, h / 2)
    pts = []
    # 左上角：圆心(r,r)，180°->270°（左->上）
    for i in range(n + 1):
        ang = math.pi + math.pi / 2 * i / n
        pts.extend([r + r * math.cos(ang), r + r * math.sin(ang)])
    # 右上角：圆心(w-r,r)，270°->360°（上->右）
    for i in range(1, n + 1):
        ang = 3 * math.pi / 2 + math.pi / 2 * i / n
        pts.extend([w - r + r * math.cos(ang), r + r * math.sin(ang)])
    # 右下角：圆心(w-r,h-r)，0°->90°（右->下）
    for i in range(1, n + 1):
        ang = math.pi / 2 * i / n
        pts.extend([w - r + r * math.cos(ang), h - r + r * math.sin(ang)])
    # 左下角：圆心(r,h-r)，90°->180°（下->左）
    for i in range(1, n + 1):
        ang = math.pi / 2 + math.pi / 2 * i / n
        pts.extend([r + r * math.cos(ang), h - r + r * math.sin(ang)])
    return pts


def _ensure_wallpaper():
    """生成一张高斯模糊的浅色简约壁纸（浅蓝白渐变 + 微噪点 + 模糊），缓存到临时目录。"""
    if os.path.exists(_WALLPAPER_PATH):
        return _WALLPAPER_PATH
    from PIL import Image, ImageFilter
    w, h = 1920, 1200
    y = np.arange(h).reshape(h, 1, 1).astype(np.float32)
    t = y / h
    b = 253 - t * 6
    g = 252 - t * 9
    r = 250 - t * 12
    arr = np.concatenate([b, g, r], axis=2)
    arr = np.broadcast_to(arr, (h, w, 3)).copy()
    noise = np.random.normal(0, 2.5, (h, w, 3)).astype(np.float32)
    arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr, "RGB")
    img = img.filter(ImageFilter.GaussianBlur(radius=12))
    img.save(_WALLPAPER_PATH, "PNG")
    return _WALLPAPER_PATH


class RoundedCard(tk.Frame):
    """圆角白色卡片：Canvas 自绘圆角矩形 + 边缘高光（模拟苹果风玻璃反光）。
    子控件请放到 .content（内部 Frame）上。"""

    def __init__(self, parent, radius=14, **kw):
        super().__init__(parent, bg=BG, highlightthickness=0, **kw)
        self.radius = radius
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.content = tk.Frame(self.canvas, bg=CARD)
        self._win = self.canvas.create_window(radius // 2, radius // 2, window=self.content, anchor="nw")
        self.canvas.bind("<Configure>", self._redraw)

    def _redraw(self, e):
        w, h = max(4, e.width), max(4, e.height)
        r = self.radius
        self.canvas.delete("card")
        # 圆角矩形主体（白色）
        pts = _rounded_pts(w, h, r)
        self.canvas.create_polygon(pts, fill=CARD, outline="", tags="card")
        # 玻璃反光：顶部/左侧近白高光，底部/右侧微灰阴影
        self.canvas.create_line(r, 2, w - r, 2, fill="#FFFFFF", width=1, tags="card")
        self.canvas.create_line(2, r, 2, h - r, fill="#F8FAFC", width=1, tags="card")
        self.canvas.create_line(r, h - 2, w - r, h - 2, fill="#E2E8F0", width=1, tags="card")
        self.canvas.create_line(w - 2, r, w - 2, h - r, fill="#E2E8F0", width=1, tags="card")
        # 调整内部 content 大小
        self.canvas.coords(self._win, r // 2, r // 2)
        self.canvas.itemconfig(self._win, width=max(1, w - r), height=max(1, h - r))



class RoundedButton(tk.Canvas):
    """圆角按钮：Canvas 自绘圆角矩形 + 文字，支持 hover/点击，避免硬边突出卡片。"""

    def __init__(self, parent, text, command, fill="#F1F5F9", fg="#475569",
                 active_fill=None, width=None, height=30, radius=8, font=UI,
                 bg=CARD, padx=14, **kw):
        if width is None:
            tw = sum(11 if ord(ch) > 127 else 6 for ch in text)
            width = tw + padx * 2
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, **kw)
        self._text = text
        self._command = command
        self._fill = fill
        self._active_fill = active_fill or fill
        self._fg = fg
        self._radius = radius
        self._font = font
        self._hover = False
        self._draw()
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _draw(self):
        self.delete("all")
        w = max(self.winfo_width(), int(self["width"]))
        h = max(self.winfo_height(), int(self["height"]))
        r = min(self._radius, w // 2, h // 2)
        fill = self._active_fill if self._hover else self._fill
        pts = _rounded_pts(w, h, r)
        self.create_polygon(pts, fill=fill, outline="")
        self.create_text(w // 2, h // 2, text=self._text, fill=self._fg, font=self._font)

    def _on_click(self, e):
        if self._command:
            self._command()

    def _on_enter(self, e):
        self._hover = True
        self._draw()

    def _on_leave(self, e):
        self._hover = False
        self._draw()

    def config(self, **kw):
        if "text" in kw:
            self._text = kw.pop("text")
            self._draw()
        if "state" in kw:
            state = kw.pop("state")
            if state == "disabled":
                self._fg = "#94A3B8"
                self._command = None
                self._draw()
        super().config(**kw)


class RoundedEntry(tk.Canvas):
    """圆角输入框：Canvas 画精确圆弧浅灰底 + 内嵌 Entry。"""

    def __init__(self, parent, textvariable, width=160, height=28, radius=8, **kw):
        super().__init__(parent, width=width, height=height, bg=CARD, highlightthickness=0)
        self._radius = radius
        self._entry = tk.Entry(self, textvariable=textvariable, font=UI, relief="flat", bd=0,
                               bg="#F8FAFC", insertbackground=TEXT, highlightthickness=0, **kw)
        self._win = self.create_window(radius + 4, height // 2, window=self._entry, anchor="w")
        self.bind("<Configure>", self._draw)
        self.after(10, self._draw)

    def _draw(self, e=None):
        self.delete("bg")
        w = max(24, self.winfo_width())
        h = max(self.winfo_height(), int(self["height"]))
        r = min(self._radius, w // 2, h // 2)
        pts = _rounded_pts(w, h, r)
        self.create_polygon(pts, fill="#F8FAFC", outline="#CBD5E1", width=1, tags="bg")
        self.tag_lower("bg")
        self.coords(self._win, r + 6, h // 2)
        self.itemconfig(self._win, width=max(4, w - 2 * r - 12))

    def focus(self):
        self._entry.focus_set()

class WatermarkApp:
    def __init__(self, root):
        self.root = root
        self.engine = WatermarkEngine()
        self.files = []
        self._q = queue.Queue()
        self._busy = False
        self._photo = None
        self._freq_photo = None
        self._thumbs = {}
        self._zoom = None          # None=适应窗口，float=缩放比
        self._freq_zoom = None     # None=适应窗口，float=缩放比
        self._selected = set()       # 多选：选中的文件路径集合
        self._last_clicked = None     # 最后点击的文件（用于Shift范围选和预览）
        self.view_mode = tk.StringVar(value="medium")   # large/medium/small/details
        self.sort_mode = tk.StringVar(value="默认")   # 默认/名称/日期/大小/格式
        self._preview_path = None
        self._resize_job = None
        self._freq_resize_job = None
        self._grid_resize_job = None
        self._known_keys = _load_known_keys() or {DEFAULT_SECRET}
        self._known_contents = _load_known_contents()

        root.title("HiddenWatermark 图片隐藏水印")
        root.geometry("1180x1000")
        root.minsize(1020, 780)
        root.configure(bg=BG)

        # 变量
        self.marker_var = tk.BooleanVar(value=True)
        self.freq_var = tk.BooleanVar(value=True)   # 频域水印默认勾选
        self.show_wm_var = tk.BooleanVar(value=True)  # 图片预览是否叠加显示水印内容
        self.owner_var = tk.StringVar(value="我的团队")
        self.note_var = tk.StringVar(value="")
        self.freq_content_var = tk.StringVar(value="")
        self.strength_var = tk.IntVar(value=40)
        self.key_var = tk.StringVar(value="my-secret-key")
        self.detect_mode_var = tk.StringVar(value="fast")
        self.output_var = tk.StringVar(value=self._desktop_dir())
        # 预览水印检测缓存：key=文件路径
        self._marker_cache = {}   # path -> 标记水印内容 dict / None / {"_error": ...}
        self._freq_cache = {}     # path -> 频域检测结果 dict / None(未命中) / {"_error": ...}

        self._build_ui()
        self._bind_preview_refresh()
        self._write_dnd_diag()
        # 窗口显示后再挂拖拽（windnd 需要有效 HWND）
        self.root.after(300, self._setup_drop)
        self.root.after(100, self._poll_queue)

    def _write_dnd_diag(self):
        """启动时把拖拽支持状态写到 %TEMP%，便于排查拖拽失效问题。"""
        try:
            info = {"frozen": bool(getattr(sys, "frozen", False)),
                    "method": "windnd(WM_DROPFILES)",
                    "has_dnd": HAS_DND,
                    "import_err": DND_IMPORT_ERR,
                    "is_elevated": self._is_elevated()}
            with open(os.path.join(tempfile.gettempdir(), "hw_dnd_diag.json"),
                      "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _is_elevated():
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # noqa: BLE001
            return None

    # ================= UI 构建 =================
    def _card(self, parent):
        return tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)

    def _section_title(self, parent, text, color=ACCENT):
        tk.Label(parent, text=text, bg=CARD, fg=color, font=UI_BOLD,
                 anchor="w").pack(fill="x", padx=14, pady=(10, 6))

    def _build_ui(self):
        # ===== 高斯模糊壁纸背景（最底层，铺满整窗）=====
        _wp_path = _ensure_wallpaper()
        self._wp_img = ImageTk.PhotoImage(Image.open(_wp_path))
        self._bg_canvas = tk.Canvas(self.root, highlightthickness=0, bg=BG)
        self._bg_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._bg_canvas.create_image(0, 0, image=self._wp_img, anchor="nw")

        # ===== 顶栏（白底 + 简约水滴 logo）=====
        header = tk.Frame(self.root, bg=HEADER_BG, highlightbackground=BORDER, highlightthickness=1)
        header.pack(fill="x")
        logo = tk.Canvas(header, width=40, height=40, bg=HEADER_BG, highlightthickness=0)
        logo.pack(side="left", padx=(16, 10), pady=7)
        logo.create_polygon(5, 12, 12, 5, 28, 5, 35, 12, 35, 28, 28, 35, 12, 35, 5, 28,
                            smooth=True, fill=ACCENT, outline="")
        logo.create_oval(16, 8, 24, 16, fill="white", outline="")
        logo.create_polygon(15, 15, 25, 15, 20, 29, fill="white", outline="")
        tk.Label(header, text="HiddenWatermark", font=("Microsoft YaHei UI", 15, "bold"),
                 fg=TEXT, bg=HEADER_BG).pack(side="left")
        tk.Label(header, text="图片隐藏水印 · 标记水印 / 频域水印可叠加", font=UI,
                 fg=MUTED, bg=HEADER_BG).pack(side="left", padx=(10, 0))
        tk.Label(header, text="支持拖拽文件至左侧列表", bg=HEADER_BG, fg=MUTED,
                 font=UI).pack(side="right", padx=16)

        # 工具栏
        toolbar = tk.Frame(self.root, bg=BG)
        tk.Label(toolbar, text="点击左侧缩略图即可自动检测该图水印并显示在预览上", bg=BG, fg=MUTED,
                 font=UI).pack(side="left")
        tk.Label(toolbar, text="可将图片/文件夹直接拖拽到左侧列表", bg=BG, fg=MUTED,
                 font=UI).pack(side="right")

        # ===== 主体：外层水平 Panedwindow（左侧功能区 | 右侧预览区），分隔条可拖拽 =====
        style = ttk.Style()
        main_pw = tk.PanedWindow(self.root, orient="horizontal", sashwidth=6, sashrelief="flat", bg=BG, showhandle=False, borderwidth=0)
        main_pw.pack(fill="both", expand=True, padx=12, pady=(8, 8))

        # ---- 左侧：垂直 Panedwindow（文件列表 | 动作 | 输出日志）----
        left_pw = tk.PanedWindow(main_pw, orient="vertical", sashwidth=6, sashrelief="flat", bg=BG, showhandle=False, borderwidth=0)
        main_pw.add(left_pw, minsize=280)

        # 文件列表卡片
        left = RoundedCard(left_pw)
        left_pw.add(left, minsize=180)
        self._card_head(left.content, "文件列表", show_count=True)

        btns = tk.Frame(left.content, bg=CARD)
        btns.pack(fill="x", padx=14, pady=(10, 4))
        RoundedButton(btns, "＋ 添加文件", self._add_files, fill=ACCENT_LIGHT, fg=ACCENT,
                      active_fill="#DBEAFE", height=28).pack(side="left", padx=(0, 6))
        RoundedButton(btns, "添加文件夹", self._add_folder, height=28).pack(side="left", padx=(0, 6))
        RoundedButton(btns, "移除选中", self._remove_selected, height=28).pack(side="left", padx=(0, 6))
        RoundedButton(btns, "清空", self._clear_files, fill="#FEE2E2", fg="#EF4444",
                      active_fill="#FECACA", height=28).pack(side="left")

        # 视图模式 + 排序 工具栏
        view_bar = tk.Frame(left.content, bg=CARD)
        view_bar.pack(fill="x", padx=14, pady=(2, 4))
        tk.Label(view_bar, text="视图：", bg=CARD, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(side="left")
        for vm, label in [("large", "大"), ("medium", "中"), ("small", "小"), ("details", "详细")]:
            RoundedButton(view_bar, label, lambda v=vm: self._on_view_change(v),
                          width=32, height=22, radius=6, font=("Microsoft YaHei UI", 9),
                          fill="#F1F5F9", fg="#475569", active_fill="#DBEAFE").pack(side="left", padx=1)
        tk.Label(view_bar, text="  排序：", bg=CARD, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(8, 0))
        sort_cb = ttk.Combobox(view_bar, textvariable=self.sort_mode, width=8, state="readonly",
                                values=["默认", "名称", "日期", "大小", "格式"],
                                font=("Microsoft YaHei UI", 9))
        sort_cb.pack(side="left", padx=(4, 0))
        sort_cb.bind("<<ComboboxSelected>>", lambda e: self._on_sort_change())

        tree_wrap = tk.Frame(left.content, bg=CARD)
        tree_wrap.pack(fill="both", expand=True, padx=14, pady=(2, 14))
        self.grid_canvas = tk.Canvas(tree_wrap, bg="#FFFFFF", highlightthickness=1,
                                     highlightbackground=BORDER, width=500)
        gsb = tk.Scrollbar(tree_wrap, command=self.grid_canvas.yview)
        self.grid_canvas.configure(yscrollcommand=gsb.set)
        self.grid_canvas.bind("<Configure>", lambda e: self._on_grid_resize())
        self.grid_canvas.bind("<Button-1>", self._on_grid_click)
        self.grid_canvas.bind("<Delete>", self._delete_selected)
        self.grid_canvas.bind("<Control-a>", self._select_all)
        self.grid_canvas.config(takefocus=1)
        gsb.pack(side="right", fill="y")
        self.grid_canvas.pack(side="left", fill="both", expand=True)

        # 动作卡片
        act = RoundedCard(left_pw)
        left_pw.add(act, minsize=210)
        self._card_head(act.content, "动作：添加隐藏水印", hint="选择要叠加的水印类型")

        arow1 = tk.Frame(act.content, bg=CARD)
        arow1.pack(fill="x", padx=14, pady=(10, 6))
        self._check(arow1, "标记水印", self.marker_var).pack(side="left")
        tk.Label(arow1, text="水印内容", bg=CARD, fg=MUTED, font=UI).pack(side="left", padx=(12, 6))
        _w1 = tk.Frame(arow1, bg=CARD)
        _w1.pack(side="left", fill="x", expand=True)
        self._entry(_w1, self.note_var, 20).pack(fill="x")

        arow2 = tk.Frame(act.content, bg=CARD)
        arow2.pack(fill="x", padx=14, pady=(0, 6))
        self._check(arow2, "频域水印", self.freq_var, cmd=self._sync_freq).pack(side="left")
        tk.Label(arow2, text="水印内容", bg=CARD, fg=MUTED, font=UI).pack(side="left", padx=(12, 6))
        _w2 = tk.Frame(arow2, bg=CARD)
        _w2.pack(side="left", fill="x", expand=True)
        self._entry(_w2, self.freq_content_var, 20).pack(fill="x")

        arow3 = tk.Frame(act.content, bg=CARD)
        arow3.pack(fill="x", padx=14, pady=(0, 6))
        tk.Label(arow3, text="强度", bg=CARD, fg=MUTED, font=UI).pack(side="left")
        _w3 = tk.Frame(arow3, bg=CARD)
        _w3.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self._strength_bar(_w3, self.strength_var).pack(fill="x")

        arow4 = tk.Frame(act.content, bg=CARD)
        arow4.pack(fill="x", padx=14, pady=(0, 12))
        tk.Label(arow4, text="密钥", bg=CARD, fg=MUTED, font=UI).pack(side="left")
        _w4 = tk.Frame(arow4, bg=CARD)
        _w4.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.key_entry = self._entry(_w4, self.key_var, 12)
        self.key_entry.pack(fill="x")
        tk.Label(arow4, text="归属人", bg=CARD, fg=MUTED, font=UI).pack(side="left", padx=(10, 6))
        _w5 = tk.Frame(arow4, bg=CARD)
        _w5.pack(side="left", fill="x", expand=True)
        self._entry(_w5, self.owner_var, 10).pack(fill="x")

        # 输出 + 日志卡片
        bottom = RoundedCard(left_pw)
        left_pw.add(bottom, minsize=150)
        self._card_head(bottom.content, "输出与运行日志")
        orow = tk.Frame(bottom.content, bg=CARD)
        orow.pack(fill="x", padx=14, pady=(8, 6))
        tk.Label(orow, text="保存位置", bg=CARD, fg=MUTED, font=UI).pack(side="left")
        tk.Entry(orow, textvariable=self.output_var, font=UI, relief="flat", bd=0, bg=SLATE50,
                 highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT).pack(
            side="left", fill="x", expand=True, ipady=5, padx=(8, 8))
        RoundedButton(orow, "浏览…", self._choose_output, height=26, width=60).pack(side="left")

        logbox = tk.Frame(bottom.content, bg=CARD)
        logbox.pack(fill="both", expand=True, padx=14, pady=(2, 14))
        self.log = tk.Text(logbox, font=MONO, bg=LOG_BG, fg=LOG_FG, relief="flat",
                           bd=0, wrap="word", state="disabled", height=2,
                           insertbackground="white", padx=8, pady=6)
        lsb = tk.Scrollbar(logbox, command=self.log.yview, bg=BORDER, troughcolor=SLATE50)
        self.log.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_config("info", foreground="#60A5FA")
        self.log.tag_config("ok", foreground="#4ADE80")
        self.log.tag_config("err", foreground="#F87171")
        self.log.tag_config("dim", foreground="#64748B")

        # ---- 右侧：仅预览窗口（图片预览 + 频域预览内嵌分栏）----
        right = RoundedCard(main_pw)
        main_pw.add(right, minsize=320)
        self._card_head(right.content, "图片预览（标记水印预览直接显示在图上）")
        zoom_bar = tk.Frame(right.content, bg=CARD)
        zoom_bar.pack(fill="x", padx=14, pady=(0, 4))
        for text, cmd in (("− 缩小", lambda: self._zoom_by(1 / 1.25)),
                          ("100%", lambda: self._set_zoom(1.0)),
                          ("＋ 放大", lambda: self._zoom_by(1.25)),
                          ("适应窗口", lambda: self._set_zoom(None))):
            RoundedButton(zoom_bar, text, cmd, height=24, width=56, radius=6,
                          font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(0, 6))
        RoundedButton(zoom_bar, "刷新检测", self._refresh_detect, height=24, width=64, radius=6,
                      font=("Microsoft YaHei UI", 9)).pack(side="right", padx=(0, 8))
        self._check(zoom_bar, "显示水印", self.show_wm_var, cmd=self._on_show_wm_toggle).pack(
            side="right", padx=(0, 10))
        tk.Label(zoom_bar, text="检测模式", bg=CARD, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(
            side="right", padx=(0, 6))
        # 分段切换控件：两个按钮连在一起，选中高亮蓝色
        mode_seg = tk.Frame(zoom_bar, bg=BORDER, highlightthickness=0)
        mode_seg.pack(side="right", padx=(0, 8))
        self._mode_fast_btn = tk.Button(mode_seg, text="快速", bg=CARD, fg=TEXT,
            font=("Microsoft YaHei UI", 9), relief="flat", bd=0, padx=10, pady=2,
            activebackground=ACCENT, activeforeground="#FFFFFF", cursor="hand2",
            command=lambda: self._set_detect_mode("fast"))
        self._mode_fast_btn.pack(side="left", padx=(1, 0), pady=1)
        self._mode_enh_btn = tk.Button(mode_seg, text="增强", bg=ACCENT, fg="#FFFFFF",
            font=("Microsoft YaHei UI", 9), relief="flat", bd=0, padx=10, pady=2,
            activebackground=ACCENT, activeforeground="#FFFFFF", cursor="hand2",
            command=lambda: self._set_detect_mode("enhanced"))
        self._mode_enh_btn.pack(side="left", padx=(0, 1), pady=1)
        self.right_pw = tk.PanedWindow(right.content, orient="vertical", sashwidth=6, sashrelief="flat", bg=BG, showhandle=False, borderwidth=0)
        self.right_pw.pack(fill="both", expand=True, padx=14, pady=(0, 4))

        pv = tk.Frame(self.right_pw, bg=CARD)
        self.right_pw.add(pv, minsize=160)
        self.preview_canvas = tk.Canvas(pv, bg="#FFFFFF", highlightthickness=1,
                                        highlightbackground=BORDER, width=900)
        self.preview_canvas.bind("<Configure>", lambda e: self._on_preview_resize())
        self.preview_info = tk.Label(pv, text="未选择文件（滚轮缩放 · 中键拖拽查看）", bg=CARD, fg=MUTED,
                                     font=UI, anchor="w")
        self.preview_info.pack(fill="x", side="bottom", padx=2, pady=(0, 4))
        self.preview_canvas.pack(fill="both", expand=True)
        self._bind_view_controls(self.preview_canvas)
        self._wm_card = tk.Frame(pv, bg="#1F2937", padx=10, pady=8)
        self._wm_card_title = tk.Label(self._wm_card, text="▍本图检测到的水印", bg="#1F2937",
                                       fg="#93C5FD", font=("Microsoft YaHei UI", 9, "bold"), anchor="w")
        self._wm_card_title.pack(fill="x")
        self._wm_card_text = tk.Label(self._wm_card, text="", bg="#1F2937", fg="#FFFFFF",
                                      font=("Microsoft YaHei UI", 9), anchor="w", justify="left")
        self._wm_card_text.pack(fill="x")
        self._wm_card.place(x=6, y=6)

        fp = tk.Frame(self.right_pw, bg=CARD)
        self.right_pw.add(fp, minsize=90)
        tk.Label(fp, text="频域水印预览（水印内容提取/预览）", bg=CARD, fg=ACCENT,
                 font=UI_BOLD, anchor="w").pack(fill="x", padx=2, pady=(0, 2))
        self.freq_canvas = tk.Canvas(fp, bg="#FFFFFF", highlightthickness=1,
                                     highlightbackground=BORDER, height=150)
        self.freq_info = tk.Label(fp, text="勾选频域水印并选择图片后，这里显示水印内容预览/提取结果", bg=CARD,
                                  fg=MUTED, font=UI, anchor="w")
        self.freq_info.pack(fill="x", side="bottom", padx=2, pady=(0, 4))
        self.freq_canvas.pack(fill="both", expand=True, padx=2, pady=(0, 2))
        self.freq_canvas.bind("<Configure>", lambda e: self._on_freq_resize())
        self._bind_view_controls(self.freq_canvas)
        self._clear_previews()

        # 底部容器：状态栏 + 开始加水印按钮（固定在底部，不被主体面板挤压裁切）
        bottom_bar = tk.Frame(self.root, bg=BG)
        bottom_bar.pack(side="bottom", fill="x")
        self.status = tk.Label(bottom_bar, text="就绪", anchor="w", bg=HEADER_BG, fg=MUTED,
                               font=UI, padx=16, pady=5, highlightbackground=BORDER,
                               highlightthickness=1)
        self.status.pack(fill="x", side="bottom")
        # 开始加水印按钮（圆角大按钮）
        self.run_btn = RoundedButton(bottom_bar, "开始加水印", self._run, fill=ACCENT, fg="white",
                                      active_fill=ACCENT_HOVER, width=180, height=38, radius=10,
                                      font=UI_BOLD, bg=BG)
        self.run_btn.pack(side="bottom", pady=(8, 10))

        # ===== 打包顺序：工具栏，最后主体 =====
        toolbar.pack(fill="x", padx=16, pady=(12, 0))
        # 初始面板比例（延迟到窗口显示后设置 sash 位置）
        self.root.after_idle(lambda: self._init_sash(main_pw, left_pw, self.right_pw))

    def _init_sash(self, main_pw, left_pw, right_pw):
        """窗口显示后设置初始面板比例。"""
        try:
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            if w > 200:
                main_pw.sashpos(0, int(w * 0.36))
            if h > 300:
                # 左侧三个面板：文件列表约40%，动作约30%，输出日志约30%
                lh = left_pw.winfo_height()
                if lh > 200:
                    left_pw.sashpos(0, int(lh * 0.40))
                    left_pw.sashpos(1, int(lh * 0.70))
                rh = right_pw.winfo_height()
                if rh > 100:
                    right_pw.sashpos(0, int(rh * 0.62))
        except Exception:
            pass




    def _card_head(self, parent, text, show_count=False, hint=None):
        """卡片标题栏（白底 + 底部分隔线）：左侧标题，右侧可选计数徽章/提示。"""
        head = tk.Frame(parent, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text=text, bg=CARD, fg=TEXT, font=UI_BOLD, anchor="w").pack(
            side="left", padx=14, pady=8)
        if hint:
            tk.Label(head, text=hint, bg=CARD, fg=MUTED,
                     font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(8, 0), pady=8)
        if show_count:
            self.count_lbl = tk.Label(head, text="0 个文件", bg="#E2E8F0", fg="#475569",
                                      font=("Microsoft YaHei UI", 9), padx=8, pady=1)
            self.count_lbl.pack(side="right", padx=14, pady=8)
        tk.Frame(head, bg=BORDER, height=1).pack(fill="x", side="bottom")

    def _check(self, parent, text, var, cmd=None):
        """简约蓝色勾选：□/☑ 自定义控件（原生 Checkbutton 勾为黑色，改用字符 + 蓝色）。"""
        box = tk.Frame(parent, bg=CARD)
        icon = tk.Label(box, text="☐", font=("Segoe UI Symbol", 12), fg="#94A3B8",
                        bg=CARD, cursor="hand2", padx=2)
        icon.pack(side="left")
        label = tk.Label(box, text=text, font=UI, fg=TEXT, bg=CARD, cursor="hand2")
        label.pack(side="left", padx=(2, 0))

        def _sync(*_):
            if var.get():
                icon.config(text="☑", fg=ACCENT)
                label.config(fg=ACCENT)
            else:
                icon.config(text="☐", fg="#94A3B8")
                label.config(fg=TEXT)

        def _toggle(_e=None):
            var.set(not var.get())
            if cmd:
                cmd()
            _sync()

        for w in (icon, label, box):
            w.bind("<Button-1>", _toggle)
        var.trace_add("write", _sync)
        _sync()
        return box

    def _entry(self, parent, var, width):
        # width 为字符数，转像素（中文约10px + 左右padding）
        px_w = width * 10 + 24
        return RoundedEntry(parent, textvariable=var, width=px_w, height=28)

    def _strength_bar(self, parent, var):
        """简约蓝色百分比条：响应式宽度，点击/拖动设置强度，右侧显示百分比。"""
        wrap = tk.Frame(parent, bg=CARD)
        bar = tk.Canvas(wrap, height=22, bg=CARD, highlightthickness=0)
        bar.pack(side="left", fill="x", expand=True)
        num = tk.Label(wrap, text="40%", width=5, bg=CARD, fg=TEXT,
                       font=("Microsoft YaHei UI", 9, "bold"), anchor="w")
        num.pack(side="left", padx=(8, 0))

        BH, RH = 22, 9  # 条高/条厚

        def _redraw(*_):
            v = int(round(var.get()))
            bar.delete("all")
            bw = max(36, bar.winfo_width())
            y0, y1 = (BH - RH) // 2, (BH + RH) // 2
            # 轨道（圆角感：矩形 + 两端圆弧）
            bar.create_rectangle(2, y0, bw - 2, y1, fill="#E2E8F0", outline="")
            bar.create_oval(2, y0, 2 + RH, y1, fill="#E2E8F0", outline="")
            bar.create_oval(bw - 2 - RH, y0, bw - 2, y1, fill="#E2E8F0", outline="")
            # 已填充（蓝色）
            fw = int((bw - 4) * v / 100)
            if fw > 0:
                bar.create_rectangle(2, y0, 2 + fw, y1, fill=ACCENT, outline="")
                bar.create_oval(2, y0, 2 + RH, y1, fill=ACCENT, outline="")
            # 滑块
            sx = 2 + fw
            bar.create_oval(sx - 6, BH // 2 - 6, sx + 6, BH // 2 + 6,
                            fill="white", outline=ACCENT, width=2)
            num.config(text=f"{v}%")

        def _set(e):
            bw = max(36, bar.winfo_width())
            v = int(round((e.x - 2) / (bw - 4) * 100))
            var.set(max(0, min(100, v)))

        bar.bind("<Button-1>", _set)
        bar.bind("<B1-Motion>", _set)
        bar.bind("<Configure>", _redraw)
        var.trace_add("write", _redraw)
        bar.after(10, _redraw)
        return wrap

    # ================= 动作标题 =================
    def _sync_freq(self):
        # 勾选频域水印时启用强度/密钥（简单起见保持可编辑，仅提示）
        pass

    # ================= 文件管理 =================
    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp *.tga"),
                       ("所有文件", "*.*")])
        self._add_paths(paths)

    def _add_folder(self):
        d = filedialog.askdirectory(title="选择文件夹")
        if d:
            self._add_paths(_collect_images(d))

    def _add_paths(self, paths):
        added = 0
        for p in paths:
            p = os.path.normpath(p)
            if os.path.isfile(p) and os.path.splitext(p)[1].lower() in IMAGE_EXTS and p not in self.files:
                self.files.append(p)
                added += 1
        if added:
            self._refresh_list(select_last=True)
            self._log(f"[提示] 已添加 {added} 个文件")

    def _remove_selected(self):
        self._delete_selected(None)

    def _delete_selected(self, _e=None):
        """删除选中的文件（带二次确认弹窗）。"""
        if not self._selected:
            # 没有选中时，删除当前预览的文件
            p = self._last_clicked if self._last_clicked in self.files else None
            if p is None and self.files:
                p = self.files[0]
            if p:
                self._selected = {p}
        if not self._selected:
            return
        # 只保留还在文件列表中的
        to_remove = [f for f in self._selected if f in self.files]
        if not to_remove:
            return
        self.grid_canvas.focus_set()
        names = "\n".join(os.path.basename(f) for f in to_remove[:5])
        more = f"\n…等共 {len(to_remove)} 个文件" if len(to_remove) > 5 else ""
        if messagebox.askyesno("确认删除", f"确定要从列表中移除以下 {len(to_remove)} 个文件吗？\n\n{names}{more}\n\n（仅从列表移除，不会删除原文件）", parent=self.root):
            for f in to_remove:
                self.files.remove(f)
            self._selected.clear()
            self._last_clicked = None
            self._refresh_list()
            self._log(f"[提示] 已移除 {len(to_remove)} 个文件")

    def _select_all(self, _e=None):
        """全选所有文件。"""
        self._selected = set(self.files)
        self._draw_grid()
        return "break"  # 阻止Canvas默认行为

    def _clear_files(self):
        self.files = []
        self._refresh_list()

    @staticmethod
    def _make_thumb(path, size=GRID_THUMB):
        try:
            im = Image.open(path)
            im.load()
            im.thumbnail((size, size), Image.LANCZOS)
            return ImageTk.PhotoImage(im)
        except Exception:
            return None

    def _selected_path(self):
        # 优先返回最后点击的文件（用于预览）
        if self._last_clicked in self.files:
            return self._last_clicked
        # 否则返回第一个选中的
        for f in self.files:
            if f in self._selected:
                return f
        return self.files[0] if self.files else None

    # ================= 左侧文件列表（多视图 + 排序） =================
    def _view_dims(self):
        """当前视图模式的尺寸参数。"""
        mode = self.view_mode.get()
        if mode == "large":
            return {"type": "grid", "thumb": 96, "cell_w": 118, "cell_h": 156, "text_h": 42}
        if mode == "medium":
            return {"type": "grid", "thumb": 64, "cell_w": 90, "cell_h": 114, "text_h": 36}
        if mode == "small":
            return {"type": "list", "thumb": 32, "row_h": 44}
        return {"type": "details", "thumb": 24, "row_h": 36}

    def _sorted_files(self):
        """按当前排序模式返回排序后的文件列表。"""
        mode = self.sort_mode.get()
        files = list(self.files)
        if mode == "名称":
            files.sort(key=lambda f: os.path.basename(f).lower())
        elif mode == "日期":
            files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        elif mode == "大小":
            files.sort(key=lambda f: os.path.getsize(f), reverse=True)
        elif mode == "格式":
            files.sort(key=lambda f: os.path.splitext(f)[1].lower())
        return files

    def _on_view_change(self, mode):
        self.view_mode.set(mode)
        self._refresh_list()

    def _on_sort_change(self):
        self._draw_grid()

    def _fmt_size(self, n):
        for unit in ["B", "KB", "MB", "GB"]:
            if n < 1024:
                return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
            n /= 1024
        return f"{n:.1f}TB"

    def _draw_grid(self):
        c = self.grid_canvas
        c.delete("all")
        if not self.files:
            c.config(scrollregion=(0, 0, 0, 0))
            return
        dims = self._view_dims()
        files = self._sorted_files()
        cw = max(c.winfo_width(), 100)
        if dims["type"] == "grid":
            self._draw_grid_mode(c, files, dims, cw)
        else:
            self._draw_list_mode(c, files, dims, cw)

    def _draw_grid_mode(self, c, files, dims, cw):
        thumb = dims["thumb"]
        cell_w = dims["cell_w"]
        cell_h = dims["cell_h"]
        text_h = dims["text_h"]
        cols = max(1, (cw - 6) // cell_w)
        rows = (len(files) + cols - 1) // cols
        total_w = max(cw, cols * cell_w)
        c.config(scrollregion=(0, 0, total_w, rows * cell_h))
        for i, f in enumerate(files):
            r, col = divmod(i, cols)
            x = col * cell_w + 8
            y = r * cell_h + 8
            tag = f"cell_{i}"
            selected = (f in self._selected)
            c.create_rectangle(x - 6, y - 6, x + cell_w - 14, y + thumb + text_h - 4,
                               fill="", outline="", tags=(tag,))
            if selected:
                c.create_rectangle(x - 6, y - 6, x + cell_w - 14, y + thumb + text_h - 4,
                                   outline=ACCENT, width=3, tags=(tag,))
            thumb_img = self._thumbs.get(f)
            if thumb_img is not None:
                c.create_image(x + thumb // 2, y + thumb // 2, image=thumb_img, tags=(tag,))
            else:
                c.create_rectangle(x, y, x + thumb, y + thumb, fill="#EFEEE9",
                                   outline=BORDER, tags=(tag,))
                c.create_text(x + thumb // 2, y + thumb // 2, text="无预览",
                              fill=MUTED, font=UI, tags=(tag,))
            name = os.path.basename(f)
            c.create_text(x + (cell_w - 14) // 2, y + thumb + 6, text=name, anchor="n",
                          width=cell_w - 14, fill=(ACCENT if selected else TEXT),
                          font=UI, tags=(tag,))

    def _draw_list_mode(self, c, files, dims, cw):
        thumb = dims["thumb"]
        row_h = dims["row_h"]
        is_details = dims["type"] == "details"
        y_offset = 28 if is_details else 4
        total_h = y_offset + len(files) * row_h + 4
        c.config(scrollregion=(0, 0, cw, total_h))
        # 详细信息列标题
        if is_details:
            c.create_rectangle(0, 0, cw, 26, fill="#F8FAFC", outline="")
            c.create_text(40, 13, text="名称", anchor="w", fill="#475569",
                          font=("Microsoft YaHei UI", 9, "bold"))
            c.create_text(cw - 220, 13, text="大小", anchor="w", fill="#475569",
                          font=("Microsoft YaHei UI", 9, "bold"))
            c.create_text(cw - 140, 13, text="格式", anchor="w", fill="#475569",
                          font=("Microsoft YaHei UI", 9, "bold"))
            c.create_text(cw - 80, 13, text="修改日期", anchor="w", fill="#475569",
                          font=("Microsoft YaHei UI", 9, "bold"))
        for i, f in enumerate(files):
            y = y_offset + i * row_h
            tag = f"cell_{i}"
            selected = (f in self._selected)
            # 行背景
            if selected:
                c.create_rectangle(2, y, cw - 2, y + row_h - 2, fill="#DBEAFE", outline="", tags=(tag,))
            elif i % 2 == 1:
                c.create_rectangle(2, y, cw - 2, y + row_h - 2, fill="#F8FAFC", outline="", tags=(tag,))
            # 缩略图
            tx = 8
            ty = y + (row_h - thumb) // 2
            thumb_img = self._thumbs.get(f)
            if thumb_img is not None:
                c.create_image(tx + thumb // 2, ty + thumb // 2, image=thumb_img, tags=(tag,))
            else:
                c.create_rectangle(tx, ty, tx + thumb, ty + thumb, fill="#EFEEE9",
                                   outline=BORDER, tags=(tag,))
            # 文件名
            name = os.path.basename(f)
            name_x = tx + thumb + 8
            name_color = ACCENT if selected else TEXT
            if is_details:
                c.create_text(name_x, y + row_h // 2, text=name, anchor="w",
                              fill=name_color, font=UI, tags=(tag,),
                              width=max(60, cw - 280))
                # 大小 / 格式 / 日期
                try:
                    sz = self._fmt_size(os.path.getsize(f))
                except OSError:
                    sz = "—"
                ext = os.path.splitext(f)[1].upper().lstrip(".") or "—"
                try:
                    mt = datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d")
                except OSError:
                    mt = "—"
                c.create_text(cw - 220, y + row_h // 2, text=sz, anchor="w",
                              fill="#475569", font=("Microsoft YaHei UI", 9), tags=(tag,))
                c.create_text(cw - 140, y + row_h // 2, text=ext, anchor="w",
                              fill="#475569", font=("Microsoft YaHei UI", 9), tags=(tag,))
                c.create_text(cw - 80, y + row_h // 2, text=mt, anchor="w",
                              fill="#475569", font=("Microsoft YaHei UI", 9), tags=(tag,))
            else:
                c.create_text(name_x, y + row_h // 2, text=name, anchor="w",
                              fill=name_color, font=UI, tags=(tag,),
                              width=max(60, cw - name_x - 10))

    def _on_grid_resize(self):
        if self._grid_resize_job:
            self.root.after_cancel(self._grid_resize_job)
        self._grid_resize_job = self.root.after(120, self._draw_grid)

    def _on_grid_click(self, e):
        c = self.grid_canvas
        c.focus_set()
        dims = self._view_dims()
        files = self._sorted_files()
        idx = None
        if dims["type"] == "grid":
            try:
                item = c.find_closest(e.x, e.y)[0]
                for t in c.gettags(item):
                    if t.startswith("cell_"):
                        idx = int(t.split("_")[1])
                        break
            except Exception:
                pass
        else:
            # 列表模式：根据 y 坐标计算行
            row_h = dims["row_h"]
            y_offset = 28 if dims["type"] == "details" else 4
            idx = (e.y - y_offset) // row_h
        if idx is None or not (0 <= idx < len(files)):
            # 点击空白处：普通点击清空选择
            if not (e.state & 0x0004) and not (e.state & 0x0001):
                self._selected.clear()
                self._last_clicked = None
                self._draw_grid()
                self._clear_previews()
            return
        clicked = files[idx]
        ctrl = bool(e.state & 0x0004)   # Ctrl键
        shift = bool(e.state & 0x0001)  # Shift键
        if shift and self._last_clicked in files:
            # Shift范围选：从最后点击的到当前点击的
            start_idx = files.index(self._last_clicked)
            end_idx = idx
            lo, hi = min(start_idx, end_idx), max(start_idx, end_idx)
            self._selected = set(files[lo:hi + 1])
        elif ctrl:
            # Ctrl加选：toggle
            if clicked in self._selected:
                self._selected.discard(clicked)
            else:
                self._selected.add(clicked)
        else:
            # 普通点击：单选
            self._selected = {clicked}
        self._last_clicked = clicked
        self._draw_grid()
        self._refresh_previews()

    def _refresh_list(self, select_last=False):
        dims = self._view_dims()
        thumb_size = dims["thumb"]
        self._thumbs.clear()
        for f in self.files:
            t = self._make_thumb(f, size=thumb_size)
            if t is not None:
                self._thumbs[f] = t
        n = len(self.files)
        sel_n = len([f for f in self._selected if f in self.files])
        if sel_n > 1:
            self.count_lbl.config(text=f"{n} 个文件，已选 {sel_n} 个")
        else:
            self.count_lbl.config(text=f"{n} 个文件")
        if not self.files:
            self._selected.clear()
            self._last_clicked = None
            self._draw_grid()
            self._clear_previews()
            return
        # 清理不在列表中的选中项
        self._selected = {f for f in self._selected if f in self.files}
        if select_last:
            self._selected = {self.files[-1]}
            self._last_clicked = self.files[-1]
        elif not self._selected:
            self._selected = {self.files[0]}
            self._last_clicked = self.files[0]
        if self._last_clicked not in self.files:
            self._last_clicked = next(iter(self._selected)) if self._selected else self.files[0]
        self._draw_grid()
        self._refresh_previews()


    # ================= 预览 =================
    def _bind_preview_refresh(self):
        # 勾选/改参数时实时刷新预览（标记叠加、频域签名分布）
        for var in (self.marker_var, self.freq_var, self.owner_var,
                    self.note_var, self.freq_content_var, self.strength_var, self.key_var):
            var.trace_add("write", lambda *_: self._refresh_previews())

    def _clear_previews(self):
        self._preview_path = None
        self.preview_canvas.delete("all")
        self.preview_canvas.config(scrollregion=(0, 0, 0, 0))
        self.preview_canvas.create_text(200, 100, text="选择文件后在此预览", fill=MUTED, font=UI)
        self.preview_info.config(text="未选择文件")
        self._hide_wm_card()
        self.freq_canvas.delete("all")
        self.freq_canvas.create_text(200, 70, text="频域水印预览", fill=MUTED, font=UI)
        self.freq_info.config(text="勾选频域水印并选择图片后，这里显示水印内容预览/提取结果")

    def _refresh_previews(self):
        p = self._selected_path()
        if not p:
            self._clear_previews()
            return
        self._render_image_preview(p)
        self._render_freq_preview(p)
        self._start_freq_detect(p)

    @staticmethod
    def _fit_size(im_size, box):
        w, h = im_size
        bw, bh = box
        if w <= bw and h <= bh:
            return (w, h)
        ratio = min(bw / w, bh / h)
        return (max(1, int(w * ratio)), max(1, int(h * ratio)))

    def _set_zoom(self, zoom):
        self._zoom = zoom
        if self._preview_path:
            self._render_image_preview(self._preview_path)

    def _zoom_by(self, factor):
        base = 1.0 if self._zoom is None else self._zoom
        self._zoom = max(0.05, min(8.0, base * factor))
        if self._preview_path:
            self._render_image_preview(self._preview_path)

    # ================= 预览画布统一交互：滚轮缩放 + 中键拖拽平移 =================
    def _bind_view_controls(self, canvas):
        canvas.bind("<MouseWheel>", lambda e, c=canvas: self._on_view_wheel(e, c))
        canvas.bind("<ButtonPress-2>", lambda e, c=canvas: self._on_pan_start(e, c))
        canvas.bind("<B2-Motion>", lambda e, c=canvas: self._on_pan_move(e, c))
        canvas.bind("<ButtonRelease-2>", lambda e, c=canvas: self._on_pan_end(e, c))

    def _on_view_wheel(self, e, canvas):
        if not self._preview_path:
            return
        factor = 1.15 ** (e.delta / 120) if e.delta else 1.1
        if canvas is self.preview_canvas:
            self._zoom_by(factor)
        elif canvas is self.freq_canvas:
            self._freq_zoom_by(factor)

    def _on_pan_start(self, e, canvas):
        canvas.scan_mark(e.x, e.y)
        canvas.configure(cursor="fleur")

    def _on_pan_move(self, e, canvas):
        canvas.scan_dragto(e.x, e.y, gain=1)

    def _on_pan_end(self, e, canvas):
        canvas.configure(cursor="")

    def _freq_zoom_by(self, factor):
        if not self._preview_path or not self.freq_var.get():
            return
        if factor > 1:  # 放大：从适应窗口开始缩放
            base = 1.0 if self._freq_zoom is None else self._freq_zoom
            self._freq_zoom = max(0.1, min(20.0, base * factor))
        else:          # 缩小：缩到 <=100% 时回到适应窗口
            if self._freq_zoom is None:
                return
            nz = self._freq_zoom * factor
            self._freq_zoom = None if nz <= 1.0 else nz
        self._render_freq_preview(self._preview_path)

    def _on_preview_resize(self):
        # 适应窗口模式下，窗口尺寸变化后自动重新适配
        if self._zoom is not None or not self._preview_path:
            return
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(
            150, lambda: self._render_image_preview(self._preview_path))

    def _on_freq_resize(self):
        # 频域画布尺寸变化（窗口缩放/拖动分栏）后重新渲染，150ms 防抖
        if not self._preview_path:
            return
        if getattr(self, "_freq_resize_job", None):
            self.root.after_cancel(self._freq_resize_job)
        self._freq_resize_job = self.root.after(
            150, lambda: self._render_freq_preview(self._preview_path))

    def _render_image_preview(self, path):
        canvas = self.preview_canvas
        canvas.delete("all")
        self._preview_path = path
        canvas.update_idletasks()
        cw = max(canvas.winfo_width(), 60)
        ch = max(canvas.winfo_height(), 60)
        try:
            im = Image.open(path)
            im.load()
            zoom = self._zoom
            if zoom is None:
                disp = self._fit_size(im.size, (cw, ch))
            else:
                disp = (max(1, int(im.width * zoom)), max(1, int(im.height * zoom)))
            disp_im = im if disp == im.size else im.resize(disp, Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(disp_im)

            # 虚拟画布 = max(视口, 图片)，图片居中；超出视口时可滚轮缩放/中键拖拽
            vw = max(cw, disp[0])
            vh = max(ch, disp[1])
            canvas.config(scrollregion=(0, 0, vw, vh))
            img_x = (vw - disp[0]) // 2
            img_y = (vh - disp[1]) // 2
            canvas.create_image(img_x, img_y, image=self._photo, anchor="nw")
            # 缩放后让视口居中于图片（避免缩到空白角）
            canvas.xview_moveto(max(0.0, (vw - cw) / (2 * vw)))
            canvas.yview_moveto(max(0.0, (vh - ch) / (2 * vh)))

            # 图片图层上方叠加显示「所选照片的真实水印内容」（不随图片缩放/移动）
            self._update_wm_card()

            kb = os.path.getsize(path) / 1024
            ztxt = "适应窗口" if zoom is None else f"{int(zoom * 100)}%"
            self.preview_info.config(
                text=f"{os.path.basename(path)}  |  {im.width}×{im.height}  |  {im.format or '?'}  |  {kb:.0f} KB  |  显示:{ztxt}")
        except Exception as e:
            canvas.delete("all")
            canvas.create_text(cw // 2, ch // 2, text=f"无法预览：{e}", fill=MUTED, font=UI)
            self.preview_info.config(text="预览失败")
            self._hide_wm_card()

    # ---------- 预览水印叠加（读所选照片的真实水印） ----------
    def _get_marker_info(self, path):
        """读取一张图的标记水印内容（读文件末尾，快）。结果缓存。"""
        if path not in self._marker_cache:
            try:
                r = self.engine.verify(path, algorithm="data_append")
                self._marker_cache[path] = r if r is not None else None
            except Exception as e:
                self._marker_cache[path] = {"_error": str(e)}
        return self._marker_cache[path]

    def _detect_freq_core(self, path, marker_info, owner, cur_key, cur_content, strength,
                          known_keys, known_contents=None, mode='enhanced'):
        """自动识别一张图的频域水印并盲提取内容（纯计算，不碰 Tk，可在工作线程调用）。

        候选密钥：标记里记录的 freq_key → 当前输入 → 历史密钥 → 默认密钥。
        候选内容：标记记录的 freq_content → 当前输入 → 归属人 → 历史水印内容。
        32 位 magic 与密钥绑定，命中不依赖候选内容；候选内容仅用于把提取图对应成
        具体文字展示（重启后靠历史内容库仍可直接读出自己加过的水印文字）。
        返回 (结果dict 或 None, 已尝试密钥列表)。
        """
        keys = []
        if marker_info and marker_info.get("freq_key"):
            keys.append(marker_info["freq_key"])
        for k in (cur_key, DEFAULT_SECRET):
            if k not in keys:
                keys.append(k)
        for k in sorted(known_keys):
            if k not in keys:
                keys.append(k)
        use_strength = int((marker_info or {}).get("freq_strength") or strength)

        # 候选内容（用于把盲提取图对应成文字展示；不影响是否命中）
        cand_raw = []
        if marker_info:
            cand_raw += [marker_info.get("freq_content"), marker_info.get("owner")]
        cand_raw += [cur_content, owner]
        for c in sorted(known_contents or []):
            cand_raw.append(c)
        cand_contents = []
        for c in cand_raw:
            c = c or ""
            if c and c not in cand_contents:
                cand_contents.append(c)

        tried = []
        for k in keys:
            tried.append(k)
            prof = WatermarkProfile(name="频域水印", algorithm="frequency_dct", owner=owner,
                                    note="", extra={"key": k, "strength": use_strength,
                                                    "cand_contents": cand_contents, "mode": mode})
            try:
                r = self.engine.verify(path, profile=prof)
            except Exception:
                r = None
            if r is not None:
                r["matched_key"] = k
                return r, tried
        return None, tried

    def _start_freq_detect(self, path):
        """后台线程检测所选照片的频域水印（DCT 较慢，不阻塞界面）。结果回主线程刷新。"""
        if path in self._freq_cache:
            return
        self._freq_cache[path] = _WM_LOADING   # 加载中（预览先显示“检测中...”）
        # 主线程先取好 Tk 变量与标记信息，工作线程不再碰 Tk
        owner = self.owner_var.get().strip() or "我的团队"
        cur_key = self.key_var.get().strip() or "my-secret-key"
        cur_content = self.freq_content_var.get().strip() or None
        strength = self.strength_var.get()
        mode = self.detect_mode_var.get()
        known = list(self._known_keys)
        known_contents = list(self._known_contents)
        marker = self._get_marker_info(path)

        def work():
            try:
                result, _tried = self._detect_freq_core(path, marker, owner, cur_key, cur_content,
                                                        strength, known, known_contents, mode=mode)
                if result is None:
                    result = {"detected": False, "tried": _tried}   # 未命中也是确定结果
            except Exception as e:
                result = {"_error": str(e)}
            self._q.put(("freq_done", path, result))

        threading.Thread(target=work, daemon=True).start()

    def _wm_overlay_lines(self):
        """收集所选照片的水印内容行（标记水印 + 频域水印）。"""
        path = self._preview_path or ""
        lines = []
        m = self._get_marker_info(path)
        if isinstance(m, dict) and m.get("_error"):
            lines.append("标记水印：读取失败")
        elif m and m.get("tampered"):
            lines.append(f"标记水印：内容被改动（{m.get('reason')}）")
        elif m:
            lines.append(f"标记水印：归属人={m.get('owner', '?')} | 水印内容={m.get('note') or '无'} | 时间={m.get('ts', '?')}")
            if m.get("_covered"):
                lines[-1] += "（被覆盖）"
        else:
            lines.append("标记水印：未检测到")

        f = self._freq_cache.get(path)
        if f is _WM_LOADING or f is None:
            lines.append("频域水印：检测中...")
        elif isinstance(f, dict) and f.get("_error"):
            lines.append("频域水印：检测失败")
        elif f and f.get("detected"):
            mc = f.get('matched_content')
            fp = f.get('fp', '?')
            if mc:
                lines.append(f"频域水印：密钥={f.get('matched_key', '?')} | 内容={mc} | ID={fp} | 匹配度={f.get('similarity', 0) * 100:.0f}%")
            else:
                lines.append(f"频域水印：已检出（密钥={f.get('matched_key', '?')}）| ID={fp} | 内容未在本机记录")
        else:
            lines.append("频域水印：未命中")
        return lines

    def _update_wm_card(self):
        """把水印信息卡固定显示在预览面板左上角（不随图片缩放/移动）。"""
        card = getattr(self, "_wm_card", None)
        if card is None:
            return
        if not self.show_wm_var.get() or not self._preview_path:
            self._hide_wm_card()
            return
        lines = self._wm_overlay_lines()
        if not lines:
            self._hide_wm_card()
            return
        self._wm_card_text.config(text="\n".join(lines))
        self._wm_card.place(x=6, y=6)

    def _hide_wm_card(self):
        card = getattr(self, "_wm_card", None)
        if card is not None:
            card.place_forget()

    def _on_show_wm_toggle(self):
        # 卡片独立于图片渲染，只需显示/隐藏，无需重画整张图
        self._update_wm_card()

    def _set_detect_mode(self, mode):
        """设置检测模式并更新分段按钮高亮状态，然后重新检测。"""
        self.detect_mode_var.set(mode)
        self._update_mode_buttons()
        self._on_detect_mode_change()

    def _update_mode_buttons(self):
        """更新分段切换按钮的高亮状态：选中=蓝底白字，未选中=灰底深字。"""
        mode = self.detect_mode_var.get()
        if mode == "fast":
            self._mode_fast_btn.config(bg=ACCENT, fg="#FFFFFF", relief="flat")
            self._mode_enh_btn.config(bg=CARD, fg=TEXT, relief="flat")
        else:
            self._mode_fast_btn.config(bg=CARD, fg=TEXT, relief="flat")
            self._mode_enh_btn.config(bg=ACCENT, fg="#FFFFFF", relief="flat")

    def _on_detect_mode_change(self):
        """切换检测模式后，清除当前预览图片的频域缓存并重新检测。"""
        if self._preview_path and self._preview_path in self._freq_cache:
            del self._freq_cache[self._preview_path]
        if self._preview_path and self.freq_var.get():
            self._start_freq_detect(self._preview_path)

    def _refresh_detect(self):
        """清空该图缓存，强制重新识别标记水印 + 频域水印（用于实时检测不准时手动兜底）。"""
        path = self._selected_path()
        if not path:
            self._log("[提示] 请先选择一张图片再刷新")
            return
        self._marker_cache.pop(path, None)
        self._freq_cache.pop(path, None)
        self._render_image_preview(path)   # 立即重新读取标记水印 + 显示“检测中...”
        self._start_freq_detect(path)      # 重新后台检测频域水印
        self._log(f"[检测] 已重新识别：{os.path.basename(path)}")

    def _render_freq_preview(self, path):
        canvas = self.freq_canvas
        canvas.delete("all")
        if not self.freq_var.get():
            self._freq_zoom = None
            canvas.create_text(210, 70, text="未勾选频域水印", fill=MUTED, font=UI)
            self.freq_info.config(text="勾选频域水印后，这里显示水印内容预览 / 提取结果")
            return
        try:
            from .algorithms.frequency_dct import render_text_wm
            owner = self.owner_var.get().strip() or "我的团队"
            content = self.freq_content_var.get().strip() or None
            cached = self._freq_cache.get(path)

            if isinstance(cached, dict) and cached.get("wm") is not None:
                # 检测到 -> 显示实际盲提取出的水印内容图
                arr = cached["wm"]
                mc = cached.get('matched_content')
                if mc:
                    info = (f"已提取频域水印：密钥={cached.get('matched_key', '?')}，"
                            f"内容={mc}，匹配度={cached.get('similarity', 0) * 100:.0f}%")
                else:
                    info = (f"已检出频域水印：密钥={cached.get('matched_key', '?')}，"
                            f"内容未在历史库中，下图为盲提取结果（可肉眼辨认）")
            elif cached is _WM_LOADING:
                arr = (render_text_wm(content or owner) * 255).astype(np.uint8)
                info = "频域水印检测中...（以下为将嵌入的内容预览）"
            else:
                # 未检测到 -> 显示将嵌入的水印内容预览
                arr = (render_text_wm(content or owner) * 255).astype(np.uint8)
                info = (f"未检测到频域水印（以下为将嵌入的内容预览：{content or owner}，"
                        f"密钥={self.key_var.get().strip() or 'my-secret-key'}）")
            im = Image.fromarray(arr, "L")
            self._render_img_to_canvas(canvas, im, info)
        except Exception as e:
            canvas.create_text(210, 70, text=f"无法预览：{e}", fill=MUTED, font=UI)
            self.freq_info.config(text="")

    def _render_img_to_canvas(self, canvas, im, info_text):
        """把 PIL 图显示到画布（适应窗口 / 滚轮缩放 / 中键拖拽），并更新频域信息栏。"""
        canvas.update_idletasks()
        cw = max(canvas.winfo_width(), 60)
        ch = max(canvas.winfo_height(), 60)
        zoom = self._freq_zoom
        if zoom is None:
            disp = self._fit_size(im.size, (cw, ch))
        else:
            disp = (max(1, int(im.width * zoom)), max(1, int(im.height * zoom)))
        disp_im = im if disp == im.size else im.resize(disp, Image.NEAREST)
        self._freq_photo = ImageTk.PhotoImage(disp_im)

        vw = max(cw, disp[0])
        vh = max(ch, disp[1])
        canvas.config(scrollregion=(0, 0, vw, vh))
        canvas.create_image((vw - disp[0]) // 2, (vh - disp[1]) // 2,
                            image=self._freq_photo, anchor="nw")
        canvas.xview_moveto(max(0.0, (vw - cw) / (2 * vw)))
        canvas.yview_moveto(max(0.0, (vh - ch) / (2 * vh)))
        ztxt = "适应窗口" if zoom is None else f"{int(zoom * 100)}%"
        self.freq_info.config(text=f"{info_text} | 显示:{ztxt} · 滚轮缩放/中键拖拽")

    # ================= 拖拽（windnd / WM_DROPFILES） =================
    def _setup_drop(self):
        if getattr(self, "_drop_hooked", False):
            return
        self._drop_hooked = True
        if not HAS_DND:
            self._log("[提示] 拖拽库不可用，请用“添加文件/添加文件夹”按钮")
            return
        try:
            windnd.hook_dropfiles(self.root, self._on_windnd_drop, force_unicode=True)
            self._log("[提示] 拖拽已启用：把图片/文件夹直接拖到窗口任意位置即可")
        except Exception as e:  # noqa: BLE001
            self._log(f"[错误] 拖拽初始化失败：{e}")

    def _on_windnd_drop(self, paths):
        """由 windnd 的原生窗口消息回调触发。

        注意：这里运行在系统 WM_DROPFILES 消息回调里，绝不能直接操作 Tk 控件
        （刷新列表/生成缩略图/写日志），否则会打断 Tk 内部状态导致闪退。
        只做纯 Python 收集，把结果放进线程安全队列，由主循环安全处理。
        """
        try:
            to_add = []
            for p in paths or []:
                p = os.path.normpath(str(p))
                for full in _collect_images(p):
                    full = os.path.normpath(full)
                    if full not in self.files and full not in to_add:
                        to_add.append(full)
            if to_add:
                self._q.put(("add_files", to_add))
            else:
                self._q.put(("log", "[提示] 拖入的内容没有可处理的图片"))
        except Exception as e:  # noqa: BLE001
            try:
                self._q.put(("log", f"[错误] 拖拽处理失败：{e}"))
            except Exception:  # noqa: BLE001
                pass

    # ================= 输出 =================
    @staticmethod
    def _desktop_dir():
        """获取 Windows 桌面路径（兼容 OneDrive 重定向），失败则退回用户目录。"""
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_DESKTOPDIRECTORY = 0x0000
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0000, None, 0, buf) == 0 and buf.value:
                return buf.value
        except Exception:  # noqa: BLE001
            pass
        d = os.path.join(os.path.expanduser("~"), "Desktop")
        return d if os.path.isdir(d) else os.path.expanduser("~")

    def _choose_output(self):
        d = filedialog.askdirectory(title="选择输出文件夹")
        if d:
            self.output_var.set(d)

    # ================= 水印组合 =================
    def _current_profiles(self):
        """根据勾选构建要应用/核对的水印档位列表（频域在前、标记在后）。

        当频域 + 标记同时勾选时，把频域密钥/强度/水印内容写进标记水印的载荷里，
        这样检测时可以直接从文件读出频域档位（无需用户手动输入核对）。
        """
        profiles = []
        owner = self.owner_var.get().strip() or "我的团队"
        freq_key = self.key_var.get().strip() or "my-secret-key"
        freq_content = self.freq_content_var.get().strip() or None
        if self.freq_var.get():
            profiles.append(WatermarkProfile(
                name="频域水印", algorithm="frequency_dct", owner=owner,
                note="", extra={"key": freq_key, "strength": self.strength_var.get(),
                                "content": freq_content}))
        if self.marker_var.get():
            extra = {}
            if self.freq_var.get():
                extra["freq_key"] = freq_key
                extra["freq_strength"] = self.strength_var.get()
                extra["freq_content"] = freq_content
            profiles.append(WatermarkProfile(
                name="标记水印", algorithm="data_append", owner=owner,
                note=self.note_var.get().strip(), extra=extra))
        return profiles

    # ================= 执行 =================
    def _run(self):
        if self._busy:
            return
        if not self.files:
            self._log("[提示] 请先添加图片（可直接拖拽到左侧列表）")
            return
        profiles = self._current_profiles()
        if not profiles:
            self._log("[提示] 请至少勾选一种水印类别（标记水印 / 频域水印）")
            return
        base = self.output_var.get().strip() or os.getcwd()
        outdir = self._auto_output_dir(base)
        self._log(f"[输出] 已新建文件夹：{outdir}")
        self._submit(lambda emit: self._task_embed(self.files, outdir, profiles, emit))

    def _auto_output_dir(self, base):
        """在保存位置下自动新建“水印输出_日期”文件夹（已存在则加序号），返回其路径。"""
        os.makedirs(base, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        cand = os.path.join(base, f"水印输出_{today}")
        n = 2
        while os.path.exists(cand):
            cand = os.path.join(base, f"水印输出_{today}_{n}")
            n += 1
        os.makedirs(cand, exist_ok=True)
        return cand

    def _task_embed(self, files, outdir, profiles, emit):
        os.makedirs(outdir, exist_ok=True)
        names = " + ".join(p.name for p in profiles)
        emit(f"> 处理 {len(files)} 个文件，应用水印：{names}")
        # 记录本次用到的频域密钥与水印内容，供以后验证时自动尝试/匹配（重启也保留）
        for p in profiles:
            if p.algorithm == "frequency_dct" and (p.extra or {}).get("key"):
                self._remember_key(p.extra["key"])
                c = (p.extra or {}).get("content")
                if c:
                    self._remember_content(c)
        ok = 0
        for src in files:
            dst = os.path.join(outdir, os.path.basename(src))
            try:
                apply_profiles(self.engine, src, dst, profiles)
                desc = " + ".join(_profile_desc(p) for p in profiles)
                emit(f"  ✓ {os.path.basename(src)} → {desc}")
                ok += 1
            except Exception as e:
                emit(f"  ✗ {os.path.basename(src)}  失败：{e}")
        emit(f"  完成 {ok}/{len(files)}")

    def _remember_key(self, key):
        if key and key not in self._known_keys:
            self._known_keys.add(key)
            _save_known_keys(self._known_keys)

    def _remember_content(self, content):
        if content and content not in self._known_contents:
            self._known_contents.add(content)
            _save_known_contents(self._known_contents)

    # ================= 线程安全日志 =================
    def _submit(self, worker):
        self._busy = True
        self.run_btn.config(state="disabled")

        def run():
            try:
                worker(self._emit)
            except Exception as e:
                self._emit(f"[错误] {e}")
            finally:
                self._q.put(("done",))

        threading.Thread(target=run, daemon=True).start()

    def _emit(self, msg):
        self._q.put(("log", msg))

    def _poll_queue(self):
        try:
            while True:
                kind, *rest = self._q.get_nowait()
                if kind == "log":
                    self._append_log(rest[0])
                elif kind == "add_files":
                    added = 0
                    for f in rest[0]:
                        if f not in self.files:
                            self.files.append(f)
                            added += 1
                    if added:
                        self._refresh_list(select_last=True)
                        self._append_log(f"[提示] 拖入 {added} 个文件")
                    self._set_status("就绪")
                elif kind == "done":
                    self._busy = False
                    self.run_btn.config(state="normal")
                    self._set_status("完成")
                elif kind == "freq_done":
                    # 频域水印后台检测完成：回写缓存并刷新预览（图片叠加 + 频域提取图）
                    path, result = rest
                    self._freq_cache[path] = result
                    if path == self._preview_path:
                        self._render_image_preview(path)
                        self._render_freq_preview(path)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _log(self, msg):
        self._append_log(msg)

    def _append_log(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        # 深色终端下按内容着色：[提示]蓝 / ✓绿 / ✗红 / 其余灰
        tag = "dim"
        if msg.startswith("[提示]") or msg.startswith("[输出]"):
            tag = "info"
        elif "✓" in msg:
            tag = "ok"
        elif "✗" in msg or "失败" in msg:
            tag = "err"
        n = self.log.index("end-1c")
        self.log.tag_add(tag, f"{float(n) - 1.0} linestart", "end-1c")
        self.log.see("end")
        self.log.config(state="disabled")

    def _set_status(self, text):
        self.status.config(text=text)


def run(smoke: bool = False):
    root = ROOT_CLS()
    WatermarkApp(root)
    if smoke:
        root.after(2000, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    run()
