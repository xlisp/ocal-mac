# OCAL 准直仪校准工具 · macOS 原生重写方案

## Context（背景）

`/Users/xlisp/CppPro/ocal-win` 目录里没有源码，只有 32 位 Windows 程序 `OCAL1.32.exe`（MFC + SkinMagic + OpenCV 3.4.7，工程名 `mfcopencv`）及配套 DLL 和配置文件。通过逆向 PE 资源（对话框模板、导入表、字符串）已完整还原其功能：

**它是一个 OCA 设备准直仪（平行光管）校准辅助工具**：
- 打开 USB 工业相机，实时显示画面（原版用 OpenCV VideoCapture + StretchDIBits）
- 在画面上叠加 **3 个校准圆 + 1 个准星**，每个可独立：启用开关、颜色、半径/长度、线条粗细；准星还有角度旋转
- **圆心偏移**：启用后所有圆/准星中心 = 画面中心 + (左右、上下) 偏移
- 按相机分辨率选择配置文件：`config720.txt / config1080.txt / config1k.txt / config4k.txt`
  - 格式（11 个整数，空格分隔）：`圈1半径 圈1启用 圈2半径 圈2启用 圈3半径 圈3启用 准星长度 准星启用 角度 上下偏移 左右偏移`
  - 例：config1080.txt = `307 1 276 1 50 1 481 1 137 7 -5`
- `focus.txt`：`0 0 1616.83 1172.13` — 保存的标定圆心坐标（两个整数占位 + 浮点 x,y），3264×2448 相机的画面中心附近
- 「摄像头设置」按钮：原版打开 DirectShow 属性页

## 技术选型：Swift + AppKit + AVFoundation（不用 C++）

理由：此 App 的核心需求是「摄像头取流 + 画面叠加绘制 + 少量控件」，Swift 方案在 macOS 上**零第三方依赖**（AVFoundation 原生支持 UVC 工业相机，本机已识别到一台 UVC 相机）；C++ 则必须引入 Qt 才能做 GUI，构建重、体积大。原版用 OpenCV 只是取流和画圆，这些 AVFoundation + CoreGraphics 都有原生等价物。

## 交付物

在 `/Users/xlisp/CppPro/ocal-win/` 下新建 `mac/` 目录，包含 Swift 源码 + 构建脚本，产物为 `OCAL.app`（ad-hoc 签名，本机可直接运行）。构建只需 Command Line Tools（本机已有 swiftc 6.2）。

## 文件结构

```
mac/
├── build.sh              # swiftc 编译 + 组装 .app bundle（含 Info.plist、ad-hoc 签名）
├── Sources/
│   ├── main.swift        # App 入口，主窗口（左侧控制面板 + 右侧视频区）
│   ├── AppDelegate.swift
│   ├── CameraService.swift   # AVCaptureSession 封装：设备枚举/选择、取流、分辨率上报
│   ├── VideoView.swift       # 自定义 NSView：绘制视频帧 + 叠加圆/准星（像素精确缩放）

