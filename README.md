# HiddenWatermark 图片隐藏水印软件

给批量图片一键添加隐藏水印，并支持验证一张图片是否带我们的水印。
图片里的所有其他信息（像素、EXIF、ICC 等）都不改变。

**两种使用方式**：
- 图形界面（推荐）：双击 `dist\HiddenWatermark.exe`，或源码运行 `python gui.py`
- 命令行：`python main.py ...`（见下文）

**当前已实现**：
- 方案二（文件级隐藏标记）——文件末尾追加标记块，字节级无损，能精确读回归属人/备注
- 方案一（DCT 频域水印）——标记融进像素内容，抗 JPEG 重压/重编码，适合"被盗用后仍能证明归属"

两种水印可叠加在同一张图上（双保险）。

## 图形界面（XnConvert 风格）

- **左侧文件列表**：支持直接把图片/文件夹**拖拽**进来，也可点按钮添加
- **右侧图片预览**：点击列表文件即可大图预览（含尺寸/格式/大小）
- **动作板块「添加隐藏水印」**：标记水印 / 频域水印是**两个独立勾选项**，可分别单独选、
  也可同时勾选叠加（双保险），不再二选一
- 支持：批量加水印 / 批量验证；后台线程处理，大文件夹不卡界面
- 频域水印的强度滑块、密钥、归属人可直接在界面上设置

## 目录结构

```
HiddenWatermark_Project/
├── main.py                  # 软件入口
├── profiles.json            # 预设水印档位（首次运行自动生成，可自行编辑）
├── demo_step1.py            # 第一步演示脚本
├── gui.py                  # 图形界面入口（python gui.py）
└── app/                     # 软件包
    ├── config.py            # 配置加载（预设档位、图片格式）
    ├── models.py            # 数据模型（WatermarkProfile / FileResult）
    ├── engine.py            # 水印引擎：统一 embed / verify 入口
    ├── batch.py             # 批量处理与批量验证
    ├── cli.py               # 命令行定义
    ├── gui.py               # 图形界面（tkinter）
    └── algorithms/          # 水印算法（可插拔）
        ├── base.py          # 算法抽象基类
        ├── data_append.py   # 方案二：文件级追加标记
        └── frequency_dct.py # 方案一：DCT 频域水印（抗压缩/重编码）
```

## 打包成 exe

```bash
pip install pyinstaller                 # 只在打包时需要
python -m PyInstaller --noconfirm --clean --onefile --windowed --name HiddenWatermark gui.py
```

产物在 `dist\HiddenWatermark.exe`，可双击直接运行，无需安装 Python。
打包后的配置文件自动放在 `%APPDATA%\HiddenWatermark\profiles.json`（源码运行则放在项目根目录）。

## 快速开始

在 VSCode 打开项目，激活环境后：

```bash
python demo_step1.py                    # 看一遍完整的测试演示
python main.py profiles                 # 查看预设水印档位
```

## 常用命令

| 命令 | 作用 |
|---|---|
| `python main.py embed 原图.png 输出.png --profile 个人专属` | 单张加水印（方案二，字节级无损） |
| `python main.py embed 原图.png 输出.png --profile "抗篡改(频域)"` | 单张加水印（方案一，抗压缩重编码） |
| `python main.py verify 图片.png --profile "抗篡改(频域)"` | 验证频域水印（需指定嵌入时的档位） |
| `python main.py verify 图片.png` | 验证方案二标记（无需指定档位） |
| `python main.py batch-embed 输入文件夹 输出文件夹 --profile "抗篡改(频域)"` | 批量加水印（保留目录结构） |
| `python main.py batch-verify 文件夹 --profile "抗篡改(频域)"` | 批量验证 |
| `python main.py batch-embed ... --overwrite` | 覆盖已存在的输出 |
| `python main.py batch-embed ... --report report.txt` | 同时写出报告文件 |

`--owner` / `--note` 可临时覆盖档位里的归属人和备注。想长期改，直接编辑 `profiles.json`。

> 频域水印验证要点：验证时必须选择**与嵌入时相同的档位**（密钥/归属人/强度一致才能匹配）。
> 方案二标记则无需档位，自动扫描即可识别。

## 如何扩展（以后接入方案一）

1. 在 `app/algorithms/` 下新建文件（如 `frequency.py`），继承 `base.WatermarkAlgorithm`，
   实现 `embed` / `verify` 两个方法；
2. 在 `app/algorithms/__init__.py` 里 `register(你的算法())`；
3. 在 `profiles.json` 里把某个档位的 `algorithm` 改成新算法名即可。

## 两种水印的区别与局限

| | 方案二：文件级标记 | 方案一：DCT 频域水印 |
|---|---|---|
| 原理 | 文件末尾追加标记块 | 亮度通道 DCT 系数嵌入签名 |
| 保真 | 字节级无损 | 像素微变（肉眼基本无感，PSNR>40） |
| 抗重编码 | ❌ 重存/压缩/截图即失效 | ✅ 抗 JPEG 重压、PNG/JPEG 重存 |
| 抗缩放 | ✅（文件不变） | ⚠️ 缩放/裁切后鲁棒性下降 |
| 验证 | 无需档位，自动识别 | 必须指定相同档位（密钥/归属人/强度） |

**建议**：重要图片可叠加两种水印（先频域后文件标记），原始文件用方案二证明"这是原版"，
内容被二次传播后用方案一证明"还是我们的"。

频域水印的强度等参数在 `profiles.json` 的 `extra` 字段里调整（`strength` 越大越抗压但越易感知）。
