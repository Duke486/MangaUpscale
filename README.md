# 漫画超分

Windows 漫画批量超分工具，支持 ZIP、CBZ 和图片型 EPUB，输出 CBZ。当前版本：5.1.1。

## 下载与使用

从 [Releases](https://github.com/Duke486/MangaUpscale/releases) 下载 `MangaUpscale-5.1.1-Windows-x64.exe` 并双击。客户端携带应用代码、基础 Python、nunif 源码和 7-Zip；首次启动从 PyTorch、PyPI、MangaJaNai 和 nunif 官方来源安装依赖、下载模型，安装过程显示进度窗口。失败后重新打开可重试，安装完成后可离线使用。

运行需要 Windows 10/11 x64、NVIDIA 显卡及兼容 CUDA 12.8 的驱动，以及约 11 GB 可用空间。首次安装需下载数 GB 的显卡依赖和模型；Release 本身不托管这些大文件。彩页使用官方 BF16 ONNX 模型，首次使用时编译引擎。

1. 选择漫画目录、保存位置和缓存位置。
2. 设置需要超分的页高范围与默认风格（传统日漫、平涂或强制全局彩页）。
3. 点击「仅检测」查看待处理数量，或点击「逐包选模型」选页对比并确认各包设置。
4. 点击「开始 / 继续」。暂停、停止后可继续，已存在的成品自动跳过。

逐包对比可选择三种风格，也可使用批次默认；确认后保存当时的实际风格，之后修改默认风格不会改变已确认的包。黑白页、彩页和保留原尺寸的预览均可确认，只处理已确认的包。

预览面板的「修改默认风格」作用于本任务当前列表内的漫画包；手动选风格、逐包确认或手动跳过的包默认保留。勾选「强制覆盖本任务所有漫画包」会清除这些单包设置并统一重做。未做单包设置的已完成漫画也会重做，原成品在新成品校验通过后替换。运行中提交的修改在当前漫画结束后应用；停止时尚未应用的修改保留到下次开始。

默认风格保存在任务缓存中，继续同一任务时沿用。新任务使用主界面所选风格。

预览支持滚轮缩放、拖动与双击适屏，两侧底部分别显示分辨率与图片大小。逐包对比的结果大小指 PNG 预览文件；正式成品使用 JPG 95 编码。

新任务保留原始包名和页名。此版本使用独立任务格式，请新建缓存任务。源漫画保持不变，成品校验通过后才发布到保存位置。

## 模型

| 默认风格 | 黑白页 | 彩页 |
| --- | --- | --- |
| 传统日漫 | MangaJaNai V1 ESRGAN，按长边选择 1200–2048p 模型，4× | IllustrationJaNai V3detail FDAT M，原生 2× |
| 平涂 | waifu2x art，降噪 2，4× | IllustrationJaNai V3detail FDAT M，原生 2× |
| 强制全局彩页 | IllustrationJaNai V3detail FDAT M，原生 2× | IllustrationJaNai V3detail FDAT M，原生 2× |

传统日漫和平涂两档均自动识别彩页；「强制全局彩页」让所有页统一走彩页模型；页高范围和跳过过大页的设置仍然生效。

自动检测会检查画面主体中的局部彩色线条、贴纸、标题和角色，并抑制页边、页角的小面积彩色水印及轻微纸张偏色。预览与正式处理使用同一套检测。继续旧任务时会从本地快照更新旧检测；分类发生变化的漫画包会重新打包，未变化的页面复用缓存，旧成品在新成品校验通过后替换。

图片型 EPUB 按 spine 页序读取，输出使用连续页码。ZIP/CBZ 保留包内阅读顺序和原始页名；需要转为 JPEG 的页面改用 `.jpg` 扩展名。

## 开发、检查与构建

使用 Windows x64、Python 3.11 或更新版本，安装 `requirements-dev.txt` 后运行：

```powershell
python -m pip install -r requirements-dev.txt
./check.ps1
```

检查包含 Ruff、合成图片回归、真实 Qt 事件循环、CPU 替身推理的打包/恢复流程，以及合成载荷的 Windows 启动器解包测试；不调用 GPU、不读取真实漫画。可用 `./check.ps1 -Python ./runtime/python/python.exe` 指定已安装开发依赖的本地解释器。

测试保留出版安全、路径校验、队列重试/停止、EPUB 页序和默认风格重做回归，删除重复的锁检查和只核对 UI 文案、常量或简单状态读取的用例。

GPU 运行库和模型不提交到源码仓库；`runtime.json` 使用相对路径，随安装目录一起移动。准备好 `runtime/python`、`runtime/nunif` 和 7-Zip 后，运行 `./packaging/build.ps1` 构建轻量单文件客户端，产物及 SHA-256 校验文件在 `dist/client/`。版本号从 `pyproject.toml` 读取，构建时清空 `build/client/`，避免旧文件混入。

发布的 exe 将程序安装到 `%LOCALAPPDATA%/MangaUpscale/5.1.1-client/`。仓库内的 `漫画超分.exe` 使用 `packaging/LocalLauncher.cs`，直接启动旁边已经准备好的运行环境；更新源码后可继续使用。

## 第三方组件

模型、推理库和界面库的来源与许可证见 [THIRD_PARTY.md](THIRD_PARTY.md)。MangaJaNai / IllustrationJaNai 模型遵循 CC BY-NC 4.0，仅限非商业使用。
