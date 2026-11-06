#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCAL 准直仪校准工具 (Collimator Calibration) — macOS 版
=====================================================

Windows 版 OCAL 1.32 (MFC + OpenCV 3.4) 的 macOS 移植。

 * 打开工业相机实时画面,叠加绘制 3 个同心圆 + 十字准星,用于光轴对准/校准
 * 圆心基准点取自 focus.txt(注意:原版读取 4 个 float,只用后两个)
 * 参数保存到 config<分辨率>.txt(11 个 int,格式与原版字节兼容)

config<res>.txt 11 个字段(按文件顺序,与原版一致):
    1  圈1 半径        2  圈1 粗细
    3  圈2 半径        4  圈2 粗细
    5  圈3 半径        6  圈3 粗细
    7  准星 长度       8  准星 粗细     9  准星 角度(单位 0.1°)
   10  圆心偏移 上/下(Y)   11  圆心偏移 左/右(X)

focus.txt: 4 个 float —— "a b fx fy",原版只保留后两个 (fx, fy) 作为圆心基准。

用法:
    python3 ocal.py            # 启动 GUI
    python3 ocal.py --selftest # 自检:格式解析/回写、绘图逻辑

依赖:python3, opencv-python, Pillow, numpy, tkinter(系统自带)
"""

import math
import sys
import threading
import time
import traceback
from pathlib import Path

import cv2
import numpy as np

import tkinter as tk
from tkinter import ttk, messagebox, colorchooser, simpledialog

APP_DIR = Path(__file__).resolve().parent
APP_TITLE = "OCAL 准直仪校准 (macOS)"

# 分辨率 → 配置文件名(与原版一致)
RESOLUTIONS = [
    ("4K  3264×2448", (3264, 2448), "config4k.txt"),
    ("1080P 1920×1080", (1920, 1080), "config1080.txt"),
    ("720P 1280×720", (1280, 720), "config720.txt"),
    ("1K  1024×768", (1024, 768), "config1k.txt"),
]
DEFAULT_CONFIG = "config4k.txt"  # 原版默认

# 11 个配置字段:(键, 说明, 默认值)
CONFIG_SPEC = [
    ("c1_r", "圈1 半径", 1500),
    ("c1_w", "圈1 粗细", 2),
    ("c2_r", "圈2 半径", 1000),
    ("c2_w", "圈2 粗细", 2),
    ("c3_r", "圈3 半径", 500),
    ("c3_w", "圈3 粗细", 2),
    ("xh_len", "准星 长度", 500),
    ("xh_w", "准星 粗细", 2),
    ("xh_ang", "准星 角度", 0),      # 单位 0.1°
    ("off_ud", "圆心偏移 上/下", 0),  # Y
    ("off_lr", "圆心偏移 左/右", 0),  # X
]
CONFIG_KEYS = [k for k, _, _ in CONFIG_SPEC]

CIRCLE_KEYS = ("c1", "c2", "c3")


def fmt_float(v):
    """float → 紧凑字符串(保持 focus.txt 原有精度风格,如 1616.83)。"""
    s = f"{v:.6g}"
    return s


# ---------------------------------------------------------------- 配置读写

def find_config_file(size):
    """按相机实际分辨率选配置文件,未知分辨率回退 config4k.txt(原版默认)。"""
    for label, (w, h), name in RESOLUTIONS:
        if size == (w, h):
            return name
    return DEFAULT_CONFIG


def load_config(path):
    """读 config<res>.txt:11 个 int,空格分隔。返回 dict。"""
    toks = Path(path).read_text(encoding="utf-8", errors="replace").split()
    if len(toks) < len(CONFIG_KEYS):
        raise ValueError(f"{Path(path).name}: 需要 {len(CONFIG_KEYS)} 个参数,只有 {len(toks)} 个")
    vals = [int(float(t)) for t in toks[:len(CONFIG_KEYS)]]  # 兼容万一写成小数
    return dict(zip(CONFIG_KEYS, vals))


def save_config(path, cfg):
    """写回与原版相同的格式:一行 11 个 int、单个空格分隔、无结尾换行。"""
    s = " ".join(str(int(cfg[k])) for k in CONFIG_KEYS)
    Path(path).write_text(s, encoding="ascii")


def load_focus(path):
    """读 focus.txt:4 个 float,原版逻辑是后两个生效。返回 (raw4, fx, fy)。"""
    toks = Path(path).read_text(encoding="utf-8", errors="replace").split()
    if len(toks) < 4:
        raise ValueError(f"{Path(path).name}: 需要 4 个浮点参数")
    raw = [float(t) for t in toks[:4]]
    return raw, raw[2], raw[3]


def save_focus(path, raw, fx, fy):
    """写 focus.txt:保持 4 个 float 的格式(前两个原样保留,后两个更新)。"""
    a = fmt_float(raw[0]) if raw[0] != 0 else "0"
    b = fmt_float(raw[1]) if raw[1] != 0 else "0"
    s = f"{a} {b} {fmt_float(fx)} {fmt_float(fy)}"
    Path(path).write_text(s, encoding="ascii")


# ---------------------------------------------------------------- 绘制(复刻原版几何)

def compute_center(fx, fy, off_lr, off_ud, off_enabled):
    """原版:勾选圆心偏移时 x = fx + 左右(v11), y = fy + 上下(v10);否则直接 (fx, fy)。"""
    if off_enabled:
        return fx + off_lr, fy + off_ud
    return fx, fy


def draw_overlay(img, cfg, colors, enables, fx, fy):
    """在帧上画 3 个同心圆 + 十字准星,几何/参数与原版一致。"""
    cx, cy = compute_center(fx, fy, cfg["off_lr"], cfg["off_ud"], enables["off"])
    # OpenCV 需要整数圆心(原版用 fistp 取整)
    cx_i, cy_i = int(round(cx)), int(round(cy))

    # --- 三个同心圆:cv::circle(img, center, r, Scalar, thickness, LINE_AA)
    for ck in CIRCLE_KEYS:
        if not enables[ck]:
            continue
        r = int(cfg[f"{ck}_r"])
        w = int(cfg[f"{ck}_w"])
        if r <= 0:
            continue
        cv2.circle(img, (cx_i, cy_i), r, colors[ck], max(1, w), cv2.LINE_AA)

    # --- 十字准星:过中心、角度 a = v9/10° 的两条互相垂直线,半长 = v7
    if enables["xh"]:
        L = float(cfg["xh_len"])
        w = max(1, int(cfg["xh_w"]))
        a = math.radians(cfg["xh_ang"] / 10.0)
        for ang in (a, a + math.pi / 2):
            dx = L * math.sin(ang)
            dy = L * math.cos(ang)
            p1 = (int(round(cx - dx)), int(round(cy - dy)))
            p2 = (int(round(cx + dx)), int(round(cy + dy)))
            cv2.line(img, p1, p2, colors["xh"], w, cv2.LINE_AA)

    # focus 点标记(白色小点,便于确认基准位置)
    cv2.drawMarker(img, (cx_i, cy_i), (255, 255, 255),
                   markerType=cv2.MARKER_CROSS, markerSize=10, thickness=1)
    return img


# ---------------------------------------------------------------- 相机线程

class CameraThread(threading.Thread):
    """后台取流线程。失败时置 self.error 并退出。"""

    def __init__(self, index=0, want_size=None):
        super().__init__(daemon=True)
        self.index = index
        self.want_size = want_size
        self.lock = threading.Lock()
        self.frame = None
        self.stop_flag = threading.Event()
        self.error = None
        self.real_size = None

    def run(self):
        try:
            cap = cv2.VideoCapture(self.index, cv2.CAP_AVFOUNDATION)
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            return
        if not cap.isOpened():
            cap.release()
            self.error = f"无法打开摄像头 {self.index}"
            return
        if self.want_size:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.want_size[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.want_size[1])
        self.real_size = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                          int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        while not self.stop_flag.is_set():
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.01)
                continue
            with self.lock:
                self.frame = frame
        cap.release()

    def get(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()

    def stop(self):
        self.stop_flag.set()


def make_test_pattern(w, h):
    """无相机时的模拟画面:棋盘格 + 中心刻度,便于校验叠加层。"""
    tile = max(16, min(w, h) // 48)
    yy, xx = np.mgrid[0:h, 0:w]
    checker = ((xx // tile) + (yy // tile)) % 2
    img = np.where(checker == 0, 48, 64).astype(np.uint8)
    img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    cv2.line(img, (w // 2 - 30, h // 2), (w // 2 + 30, h // 2), (96, 96, 96), 1)
    cv2.line(img, (w // 2, h // 2 - 30), (w // 2, h // 2 + 30), (96, 96, 96), 1)
    cv2.putText(img, f"TEST {w}x{h}", (16, 32),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (128, 128, 128), 2)
    return img


# ---------------------------------------------------------------- 主应用

class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_TITLE)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 状态
        self.cfg = {k: d for k, _, d in CONFIG_SPEC}
        self.enables = {"c1": True, "c2": True, "c3": True, "xh": True, "off": True}
        self.colors = {"c1": (0, 0, 255),   # 红
                       "c2": (0, 255, 0),   # 绿
                       "c3": (255, 0, 0),   # 蓝
                       "xh": (255, 255, 255)}  # 白
        self.focus_raw = [0.0, 0.0]     # focus.txt 前两个值(原版丢弃,保存时原样写回)
        self.fx, self.fy = 0.0, 0.0
        self.focus_dirty = False
        self.cam = None
        self.cam_size = None           # (w, h) 实际画面尺寸
        self.test_mode = False
        self.config_name = DEFAULT_CONFIG
        self._ui_vars = {}
        self._closing = False

        self._build_ui()

        # 启动时加载默认配置(focus.txt + config4k.txt)
        self.load_all_configs(DEFAULT_CONFIG)

        # 视频窗口
        self.video_win = None
        self._video_label = None
        self._photo = None
        self.open_video_window()

        self.root.after(33, self._tick)

    # ---------------------------------------------------------- UI 构建

    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        # -- 摄像头 --
        f_cam = ttk.LabelFrame(outer, text="摄像头", padding=6)
        f_cam.pack(fill="x", **pad)
        ttk.Button(f_cam, text="开启摄像头", width=12, command=self.cam_start).pack(side="left", padx=3)
        ttk.Button(f_cam, text="关闭摄像头", width=12, command=self.cam_stop).pack(side="left", padx=3)
        ttk.Button(f_cam, text="摄像头设置", width=12, command=self.cam_settings).pack(side="left", padx=3)
        ttk.Button(f_cam, text="关闭程序", width=12, command=self.on_close).pack(side="left", padx=3)
        self.var_res = tk.StringVar(value="未开启")
        ttk.Label(f_cam, textvariable=self.var_res).pack(side="left", padx=12)

        # -- 圆心偏移 --
        f_off = ttk.LabelFrame(outer, text="圆心偏移(focus 点 ± 偏移)", padding=6)
        f_off.pack(fill="x", **pad)
        self._add_check(f_off, "off", "启 用 圆 心 偏 移", row=0, col=0, colspan=4)
        self._add_param(f_off, "off_ud", "上  下 (Y)", -2000, 2000, row=1, col=0, colspan=3)
        self._add_param(f_off, "off_lr", "左  右 (X)", -2000, 2000, row=2, col=0, colspan=3)
        ttk.Label(f_off, text="focus 点 (fx, fy):").grid(row=1, column=3, sticky="e")
        self.var_focus = tk.StringVar(value="—")
        ttk.Label(f_off, textvariable=self.var_focus).grid(row=2, column=3, sticky="w")

        # -- 圈1/2/3 --
        self._circle_frames = {}
        for i, ck in enumerate(CIRCLE_KEYS):
            f = ttk.LabelFrame(outer, text=f"圈{i + 1} 设置", padding=6)
            f.pack(fill="x", **pad)
            self._circle_frames[ck] = f
            self._add_check(f, ck, "启 用", row=0, col=0)
            self._add_color(f, ck, f"颜 色", row=0, col=1)
            self._add_param(f, f"{ck}_r", "半 径", 0, 2500, row=1, col=0, colspan=2)
            self._add_param(f, f"{ck}_w", "粗 细", 1, 20, row=2, col=0, colspan=2)

        # -- 准星 --
        f_xh = ttk.LabelFrame(outer, text="准星设置(十字线)", padding=6)
        f_xh.pack(fill="x", **pad)
        self._add_check(f_xh, "xh", "启 用", row=0, col=0)
        self._add_color(f_xh, "xh", "颜 色", row=0, col=1)
        self._add_param(f_xh, "xh_len", "长 度", 0, 2500, row=1, col=0, colspan=2)
        self._add_param(f_xh, "xh_w", "粗 细", 1, 10, row=2, col=0, colspan=2)
        self._add_param(f_xh, "xh_ang", "角 度 (0.1°)", 0, 3600, row=3, col=0, colspan=2,
                        fmt=lambda v: f"{v / 10:.1f}°")

        # -- 保存 --
        f_save = ttk.Frame(outer)
        f_save.pack(fill="x", pady=8)
        ttk.Button(f_save, text="保存参数", command=self.save_params).pack(side="left", padx=3)
        ttk.Button(f_save, text="重载配置", command=self.reload_params).pack(side="left", padx=3)
        self.var_cfgfile = tk.StringVar(value="")
        ttk.Label(f_save, textvariable=self.var_cfgfile).pack(side="left", padx=10)

        ttk.Label(outer, text="提示:视频窗口中点按鼠标左键可把 focus 点移到该处(写入 focus.txt 需点“保存参数”)",
                  foreground="#888").pack(fill="x")

    def _add_check(self, parent, key, text, row=0, col=0, colspan=1):
        v = tk.BooleanVar(value=self.enables[key])
        ttk.Checkbutton(parent, text=text, variable=v,
                        command=lambda: self._on_check(key, v)).grid(
            row=row, column=col, columnspan=colspan, sticky="w")
        self._ui_vars[("chk", key)] = v

    def _on_check(self, key, v):
        self.enables[key] = bool(v.get())

    def _add_color(self, parent, key, text, row, col):
        btn = tk.Button(parent, text=text, width=6, relief="groove",
                        bg=self._rgb_hex(key),
                        command=lambda: self._pick_color(key, btn))
        btn.grid(row=row, column=col, padx=6, sticky="w")

    def _rgb_hex(self, key):
        b, g, r = self.colors[key]
        return f"#{r:02x}{g:02x}{b:02x}"

    def _pick_color(self, key, btn):
        rgb = (self.colors[key][2], self.colors[key][1], self.colors[key][0])
        res = colorchooser.askcolor(color="#%02x%02x%02x" % rgb, parent=self.root,
                                     title="选择颜色")
        if res and res[0]:
            r, g, b = [int(c) for c in res[0]]
            self.colors[key] = (b, g, r)
            btn.configure(bg=self._rgb_hex(key))

    def _add_param(self, parent, key, label, lo, hi, row, col, colspan=2, fmt=None):
        """一行参数:Label + 滑条 + 输入框(滑条和输入框双向同步,同原版)。"""
        var = tk.StringVar()
        self._ui_vars[("var", key)] = (var, lo, hi, fmt)
        guard = {"busy": False}

        def set_var_disp(val):
            var.set(str(val) if fmt is None else fmt(val))

        def on_slider(sv):
            if guard["busy"]:
                return
            guard["busy"] = True
            try:
                val = int(float(sv))
                val = max(lo, min(hi, val))
                self.cfg[key] = val
                set_var_disp(val)
            finally:
                guard["busy"] = False

        def on_entry(*_):
            if guard["busy"]:
                return
            guard["busy"] = True
            try:
                try:
                    val = int(float(var.get()))
                except ValueError:
                    return
                val = max(lo, min(hi, val))
                self.cfg[key] = val
                slider.set(val)
                set_var_disp(val)
            finally:
                guard["busy"] = False

        ttk.Label(parent, text=label, width=12).grid(row=row, column=col, sticky="e")
        slider = ttk.Scale(parent, from_=lo, to=hi, orient="horizontal",
                           command=on_slider, length=180)
        slider.grid(row=row, column=col + 1, columnspan=colspan - 1, sticky="ew", padx=4)
        ent = ttk.Entry(parent, textvariable=var, width=10)
        ent.grid(row=row, column=col + colspan, sticky="w")
        var.trace_add("write", on_entry)
        parent.columnconfigure(col + 1, weight=1)

    def _sync_ui(self):
        """把 self.cfg 状态刷到滑条/输入框(加载配置后调用)。"""
        for (kind, key), obj in self._ui_vars.items():
            if kind == "var":
                var, lo, hi, fmt = obj
                val = self.cfg[key]
                var.set(str(val) if fmt is None else fmt(val))
            else:
                obj.set(self.enables[key])
        self.var_focus.set(f"({fmt_float(self.fx)}, {fmt_float(self.fy)})")
        self.var_cfgfile.set(self.config_name)

    # ---------------------------------------------------------- 配置装载

    def config_path(self, name=None):
        # 优先应用目录(与 exe 同目录行为一致),其次当前目录
        name = name or self.config_name
        for d in (APP_DIR, Path.cwd()):
            p = d / name
            if p.exists():
                return p
        return APP_DIR / name

    def load_all_configs(self, config_name):
        self.config_name = config_name
        p = self.config_path(config_name)
        if p.exists():
            try:
                self.cfg.update(load_config(p))
            except Exception as e:  # noqa: BLE001
                messagebox.showerror(APP_TITLE, f"读取 {config_name} 失败:\n{e}")
        else:
            # 没有配置文件 → 用默认值
            self.cfg = {k: d for k, _, d in CONFIG_SPEC}
        fp = self.config_path("focus.txt")
        if fp.exists():
            try:
                raw, fx, fy = load_focus(fp)
                self.focus_raw, self.fx, self.fy = raw, fx, fy
                self.focus_dirty = False
            except Exception as e:  # noqa: BLE001
                messagebox.showerror(APP_TITLE, f"读取 focus.txt 失败:\n{e}")
        else:
            # 无 focus.txt → 默认画面中心
            w, h = self.cam_size or (3264, 2448)
            self.focus_raw = [0.0, 0.0]
            self.fx, self.fy = w / 2.0, h / 2.0
            self.focus_dirty = False
        self._sync_ui()

    def reload_params(self):
        self.load_all_configs(self.config_name)

    def save_params(self):
        """保存参数:config<res>.txt(11 int)+ focus.txt(若 focus 点被修改过)。"""
        try:
            cp = self.config_path(self.config_name)
            save_config(cp, self.cfg)
            msgs = [f"{cp}  ← 11 个参数已保存"]
            if self.focus_dirty:
                fp = self.config_path("focus.txt")
                save_focus(fp, self.focus_raw, self.fx, self.fy)
                self.focus_dirty = False
                msgs.append(f"{fp}  ← focus 点已保存")
            self.var_cfgfile.set(self.config_name)
            messagebox.showinfo(APP_TITLE, "\n".join(msgs))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"保存失败:\n{e}\n{traceback.format_exc()}")

    # ---------------------------------------------------------- 相机

    def cam_start(self):
        idx = getattr(self, "_cam_index", 0)
        want = getattr(self, "_cam_want_size", None)
        self.cam_stop()
        cam = CameraThread(idx, want)
        cam.start()
        # 等待打开结果
        for _ in range(60):
            if cam.error or cam.real_size:
                break
            time.sleep(0.05)
        if cam.error:
            cam.stop()
            self.var_res.set("未开启")
            if messagebox.askyesno(
                    APP_TITLE,
                    "未检测到硬件,请检查连接状态后重新打开应用程序。\n\n"
                    "要使用模拟画面继续吗?(用于没有工业相机时调试参数)"):
                self.test_mode = True
                w, h = want or (1920, 1080)
                self.cam_size = (w, h)
                self.cam = None
                self._after_camera_open((w, h))
            return
        self.test_mode = False
        self.cam = cam
        self._after_camera_open(cam.real_size)

    def _after_camera_open(self, size):
        self.cam_size = (int(size[0]), int(size[1]))
        name = find_config_file(self.cam_size)
        if name != self.config_name:
            self.load_all_configs(name)
        else:
            self._sync_ui()
        self.var_res.set(f"分辨率 {self.cam_size[0]}×{self.cam_size[1]}"
                         + ("(模拟)" if self.test_mode else ""))

    def cam_stop(self):
        if self.cam:
            self.cam.stop()
            self.cam.join(timeout=1.0)
            self.cam = None
        self.var_res.set("未开启" if not self.test_mode else "模拟画面")

    def cam_settings(self):
        win = tk.Toplevel(self.root)
        win.title("摄像头设置")
        win.transient(self.root)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="摄像头编号").grid(row=0, column=0, sticky="w")
        var_idx = tk.StringVar(value=str(getattr(self, "_cam_index", 0)))
        cb_idx = ttk.Combobox(frm, textvariable=var_idx, width=6,
                              values=["0", "1", "2", "3"], state="readonly")
        cb_idx.grid(row=0, column=1, sticky="w", padx=8)

        ttk.Label(frm, text="分辨率").grid(row=1, column=0, sticky="w")
        var_res = tk.StringVar(
            value=[r[0] for r in RESOLUTIONS if r[1] == getattr(self, "_cam_want_size", None)][0]
            if any(r[1] == getattr(self, "_cam_want_size", None) for r in RESOLUTIONS) else "自动")
        cb_res = ttk.Combobox(frm, textvariable=var_res, width=14,
                              values=["自动"] + [r[0] for r in RESOLUTIONS], state="readonly")
        cb_res.grid(row=1, column=1, sticky="w", padx=8)

        def apply():
            self._cam_index = int(var_idx.get())
            self._cam_want_size = None if var_res.get() == "自动" else \
                dict((r[0], r[1]) for r in RESOLUTIONS)[var_res.get()]
            win.destroy()
            self.cam_start()

        ttk.Button(frm, text="应用并打开", command=apply).grid(row=2, column=0, columnspan=2, pady=10)

    # ---------------------------------------------------------- 视频窗口

    def open_video_window(self):
        if self.video_win is not None and self.video_win.winfo_exists():
            self.video_win.lift()
            return
        win = tk.Toplevel(self.root)
        win.title("OCAL 实时画面")
        win.protocol("WM_DELETE_WINDOW", lambda: setattr(self, "video_win", None))
        win.geometry("+%d+%d" % (
            max(80, self.root.winfo_rootx() + 420),
            max(40, self.root.winfo_rooty())))
        self._video_label = ttk.Label(win)
        self._video_label.pack(fill="both", expand=True)
        self._status_var = tk.StringVar(value="等待画面…")
        ttk.Label(win, textvariable=self._status_var, anchor="w",
                  relief="groove").pack(fill="x")
        self._video_label.bind("<Button-1>", self._on_video_click)
        self.video_win = win

    def _on_video_click(self, event):
        """在视频窗口点左键 → focus 点移到该处(focus 点是关键!)。"""
        if not self._photo or not self.cam_size:
            return
        # 屏幕 → 原图坐标
        sx = self._photo.width()
        sy = self._photo.height()
        w, h = self.cam_size
        fx = event.x * w / sx
        fy = event.y * h / sy
        self.fx, self.fy = fx, fy
        self.focus_dirty = True
        self.var_focus.set(f"({fmt_float(self.fx)}, {fmt_float(self.fy)})")

    def _tick(self):
        if self._closing:
            return
        try:
            self._update_video()
        except Exception:  # noqa: BLE001
            pass
        self.root.after(33, self._tick)

    def _update_video(self):
        if self.video_win is None or not self.video_win.winfo_exists():
            self.video_win = None
            return
        if self.test_mode:
            w, h = self.cam_size or (1920, 1080)
            frame = make_test_pattern(w, h)
        elif self.cam:
            frame = self.cam.get()
            if frame is None:
                self._status_var.set("等待画面…")
                return
        else:
            self._status_var.set("摄像头未开启(点“开启摄像头”或用模拟画面)")
            if self._video_label:
                self._video_label.configure(image="")
            return

        draw_overlay(frame, self.cfg, self.colors, self.enables, self.fx, self.fy)

        # 缩放显示(最大 1280 宽 / 820 高,保持比例)
        w, h = self.cam_size
        scale = min(1280 / w, 820 / h, 1.0)
        if scale < 1.0:
            disp = cv2.resize(frame, (int(w * scale), int(h * scale)),
                             interpolation=cv2.INTER_AREA)
        else:
            disp = frame

        from PIL import Image, ImageTk
        rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb)
        self._photo = ImageTk.PhotoImage(pil)
        self._video_label.configure(image=self._photo)

        cx, cy = compute_center(self.fx, self.fy, self.cfg["off_lr"],
                                self.cfg["off_ud"], self.enables["off"])
        self._status_var.set(
            f"画面 {w}×{h} | focus ({fmt_float(self.fx)}, {fmt_float(self.fy)})"
            f" | 绘制中心 ({fmt_float(cx)}, {fmt_float(cy)})"
            f" | 配置 {self.config_name}")

    # ---------------------------------------------------------- 退出

    def on_close(self):
        self._closing = True
        self.cam_stop()
        self.root.destroy()


# ---------------------------------------------------------------- 自检

def selftest():
    """不依赖相机/GUI:验证格式解析、字节级回写、绘图几何。"""
    import tempfile
    ok = True

    src = Path("/Users/xlisp/Desktop/ocal-win")
    print("== 1. 解析原版配置文件 ==")
    expect = {
        "config4k.txt": [1500, 2, 1000, 2, 500, 2, 500, 2, 0, 0, 0],
        "config1080.txt": [307, 1, 276, 1, 50, 1, 481, 1, 137, 7, -5],
        "config720.txt": [469, 1, 637, 1, 599, 1, 365, 1, 444, 100, 100],
        "config1k.txt": [150, 1, 100, 1, 50, 1, 100, 1, 0, 0, 0],
    }
    for name, exp in expect.items():
        cfg = load_config(src / name)
        got = [cfg[k] for k in CONFIG_KEYS]
        flag = "✓" if got == exp else "✗"
        if got != exp:
            ok = False
        print(f"  {flag} {name}: {got}")

    print("== 2. focus.txt(只用后两个值) ==")
    raw, fx, fy = load_focus(src / "focus.txt")
    print(f"  raw={raw} → focus=({fx}, {fy})")
    assert (fx, fy) == (1616.83, 1172.13), "focus 解析错误"
    print("  ✓ focus = (1616.83, 1172.13)")

    print("== 3. 字节级回写兼容 ==")
    with tempfile.TemporaryDirectory() as td:
        for name in expect:
            cfg = load_config(src / name)
            out = Path(td) / name
            save_config(out, cfg)
            a = (src / name).read_bytes()
            b = out.read_bytes()
            flag = "✓" if a == b else "✗"
            if a != b:
                ok = False
                print(f"  {flag} {name}: 原 {a!r} vs 新 {b!r}")
            else:
                print(f"  {flag} {name} 字节一致")
        # focus.txt 回写(保留前两个)
        out = Path(td) / "focus.txt"
        save_focus(out, raw, fx, fy)
        b = out.read_bytes()
        a = (src / "focus.txt").read_bytes()
        flag = "✓" if a == b else "✗"
        if a != b:
            ok = False
            print(f"  {flag} focus.txt: 原 {a!r} vs 新 {b!r}")
        else:
            print("  {flag} focus.txt 字节一致".replace("{flag}", "✓"))

    print("== 4. 绘图几何(4K 默认参数) ==")
    cfg = dict(zip(CONFIG_KEYS, expect["config4k.txt"]))
    img = np.zeros((2448, 3264, 3), np.uint8)
    enables = {"c1": True, "c2": True, "c3": True, "xh": True, "off": True}
    colors = {"c1": (0, 0, 255), "c2": (0, 255, 0), "c3": (255, 0, 0), "xh": (255, 255, 255)}
    draw_overlay(img, cfg, colors, enables, 1616.83, 1172.13)
    # 圆心像素应为各颜色混合(圆与线都经过)
    px = img[1172, 1616]
    print(f"  中心像素 BGR={tuple(int(v) for v in px)}(应为非零,圆+十字叠加)")
    if not px.any():
        ok = False
    # 圈1 半径 1500:检查 (1616+1500, 1172) 处有红圈痕迹
    edge = img[1172, 1616 + 1500]
    print(f"  圈1边缘像素 BGR={tuple(int(v) for v in edge)}(应为红通道)")
    if edge[2] == 0:
        ok = False
    print()
    print("自检" + ("通过 ✅" if ok else "存在问题 ❌"))
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        return selftest()
    root = tk.Tk()
    try:
        root.tk.call("::tk::scaling")  # 确保 Tk 正常
    except Exception:  # noqa: BLE001
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())