# 漫画超分

Windows 漫画批量超分工具，支持 ZIP、CBZ 和图片型 EPUB，输出 CBZ。

## 下载与使用

从 [Releases](https://github.com/Duke486/MangaUpscale/releases) 下载 `MangaUpscale-5.0.0-Windows-x64.exe`，双击启动。无需安装 Python 或手动配置模型。首次启动自动从本仓库 Release 下载并校验运行库，随后在 `%LOCALAPPDATA%/MangaUpscale/5.0.0` 解包运行环境，后续直接打开窗口，可离线运行。下载失败后可重新打开，已完成分卷会复用。也可以将 Release 中的运行库分卷与 exe 放在同一目录，离线完成首次解包。运行需要 Windows 10/11 x64、支持 CUDA 12.8 的 NVIDIA 显卡及驱动，首次解包至少需要 11 GB 可用空间。彩页引擎首次使用时会编译并缓存。

1. 选择漫画目录、保存位置和缓存位置。
2. 设置需要超分的页高范围与批次默认画风。
3. 点击「仅检测」查看待处理数量，或点击「逐包选模型」选页对比并确认各包设置。
4. 点击「开始 / 继续」。暂停、停止后可继续，已存在的成品自动跳过。

预览支持滚轮缩放、拖动与双击适屏，两侧底部分别显示分辨率与图片大小。逐包对比中的结果大小指 PNG 预览文件；正式成品使用 JPG 95 编码。

新任务保留原始包名和页名。此版本使用独立任务格式，请新建缓存任务。源漫画保持不变，成品校验通过后才发布到保存位置。

## 模型

| 用途 | 模型 |
| --- | --- |
| 传统黑白漫画 | MangaJaNai V1 ESRGAN，按长边选择 1200–2048p 模型，4× |
| 平涂黑白漫画 | waifu2x art，降噪 2，4× |
| 彩页 | IllustrationJaNai V3detail FDAT M，原生 2× |

图片型 EPUB 按 spine 页序读取，输出使用连续页码。ZIP/CBZ 保留包内阅读顺序和原始页名；需要转为 JPEG 的页面改用 `.jpg` 扩展名。

## 开发与检查

使用 Python 3.11 或更新版本，安装 `requirements-dev.txt` 后运行：

```powershell
python -m ruff check .
python -m ruff format --check .
python -B -m unittest discover -s tests -v
python -B backend/selftest.py
```

测试使用合成图片。GPU 运行库和模型不提交到源码仓库；`runtime.json` 使用相对路径，随安装目录一起移动。单文件构建脚本见 `packaging/build.ps1`。

## 第三方组件

模型、推理库和界面库的来源与许可证见 [THIRD_PARTY.md](THIRD_PARTY.md)。MangaJaNai / IllustrationJaNai 模型遵循 CC BY-NC 4.0，仅限非商业使用。
