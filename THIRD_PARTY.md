# 第三方组件

5.1.1 轻量客户端随包携带应用代码、基础 Python、nunif 源码和 7-Zip；GPU 依赖及模型在首次安装时从上游下载。各组件的许可证和模型用途限制同样适用于安装后的本地文件。

- MangaJaNai / IllustrationJaNai，由 the-database 提供，模型许可证为 CC BY-NC 4.0，仅限非商业使用。来源：[MangaJaNai](https://github.com/the-database/MangaJaNai)。传统日漫使用 V1 ESRGAN 权重，彩页及强制全局彩页风格使用 V3detail FDAT M BF16 ONNX 模型；不把黑白页着色。模型从上游下载，配置中的 SHA-256 用于校验；许可证全文随附于 `licenses/`。
- nunif / waifu2x，由 nagadomi 提供。来源：[nunif](https://github.com/nagadomi/nunif)。客户端所附源码的 MIT 许可证与 NOTICE 保留于运行库目录，waifu2x 权重在首次安装时下载。
- PySide6 / Qt for Python：[许可证说明](https://doc.qt.io/qtforpython-6/licenses.html)。首次安装时安装独立动态链接库，保留其 LGPL/GPL 和第三方许可证，可自行替换兼容的库文件。
- PyTorch：[项目及许可证](https://github.com/pytorch/pytorch)。首次安装时从官方索引安装，许可证保留于安装目录。
- TensorRT / CUDA / cuDNN：首次安装时安装 NVIDIA 运行组件，保留包内 NVIDIA 许可证。来源：[TensorRT](https://developer.nvidia.com/tensorrt)。
- Python、Pillow、NumPy、spandrel 与其依赖：各自许可证保留于基础 Python、安装元数据和库目录。
- 7-Zip 解包器：[7-Zip](https://www.7-zip.org/)，LGPL 与 BSD，随附许可证及源码获取地址。

应用源码与第三方组件分别遵循各自许可证。随附许可证原文保存在 `licenses/` 和运行库目录。
