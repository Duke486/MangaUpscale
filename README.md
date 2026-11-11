# 漫画超分

Windows 漫画批量超分工具，支持 ZIP、CBZ 和图片型 EPUB，输出 CBZ。

## 下载与使用

从 [Releases](https://github.com/Duke486/MangaUpscale/releases) 下载 exe 并双击。客户端只携带应用代码和基础 Python；首次启动从 PyTorch、PyPI、MangaJaNai 和 nunif 官方来源安装依赖、下载模型，安装过程会显示进度窗口。失败后可重新打开重试，后续可离线使用。

运行需要 Windows 10/11 x64、NVIDIA 显卡及兼容 CUDA 12.8 的驱动，以及约 11 GB 可用空间。首次安装仍需下载数 GB 的显卡依赖和模型；Release 本身不托管这些大文件。彩页使用官方 BF16 ONNX 模型，首次使用时编译引擎。

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
