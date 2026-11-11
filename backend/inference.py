from control import check
import numpy as np


class Engine:
    def __init__(self, resource_root=None):
        self.resource_root = resource_root
        self.active = None
        self.model = None
        self.trt = None

    def upscale(self, image, model, rule, color):
        if rule.get("engine") == "tensorrt":
            return self.trt_upscale(image, model, rule, color)
        if self.trt is not None:
            self.trt = None
            __import__("gc").collect()
            __import__("torch").cuda.empty_cache()
        if rule.get("engine") == "waifu2x":
            return self.flat_upscale(image, model, rule, color)
        import gc, torch
        from PIL import Image
        from spandrel import ModelLoader

        if rule.get("engine", "spandrel") != "spandrel":
            raise ValueError("unsupported_engine")
        precision = model.get("precision", "float32")
        dtype = {"float32": torch.float32, "float16": torch.float16, "bfloat16": torch.bfloat16}[
            precision
        ]
        key = (model["path"], precision)
        if key != self.active:
            self.model = None
            gc.collect()
            torch.cuda.empty_cache()
            torch.set_num_threads(4)
            torch.backends.cuda.matmul.allow_tf32 = False
            torch.backends.cudnn.allow_tf32 = False
            self.model = ModelLoader().load_from_file(model["path"]).eval().cuda()
            if self.model.scale != 4:
                raise ValueError("model_is_not_native_4x")
            if precision == "float16" and not self.model.supports_half:
                raise ValueError("fp16_unsupported")
            if precision == "bfloat16" and not self.model.supports_bfloat16:
                raise ValueError("bf16_unsupported")
            self.model.model.to(dtype=dtype)
            if model.get("channels_last", False):
                self.model.model.to(memory_format=torch.channels_last)
            self.active = key
        core = int(rule.get("tile", 192 if color else 384))
        pad = int(rule.get("padding", 64 if color else 96))
        if not 32 <= core <= 2048 or not 16 <= pad <= 512:
            raise ValueError("invalid_tile_parameters")
        a = np.asarray(image.convert("RGB"), dtype=np.float32) / 255
        h, w = a.shape[:2]
        out = np.empty((h * 4, w * 4, 3), dtype=np.uint8)
        with torch.inference_mode():
            for y in range(0, h, core):
                for x in range(0, w, core):
                    check()
                    ye, xe = min(y + core, h), min(x + core, w)
                    y0, x0 = max(y - pad, 0), max(x - pad, 0)
                    y1, x1 = min(ye + pad, h), min(xe + pad, w)
                    t = (
                        torch.from_numpy(a[y0:y1, x0:x1].copy())
                        .permute(2, 0, 1)
                        .unsqueeze(0)
                        .to("cuda", dtype=dtype)
                    )
                    if model.get("channels_last", False):
                        t = t.to(memory_format=torch.channels_last)
                    v = self.model(t)[
                        0, :, (y - y0) * 4 : (ye - y0) * 4, (x - x0) * 4 : (xe - x0) * 4
                    ]
                    if not torch.isfinite(v).all():
                        raise ValueError("nonfinite_output")
                    out[y * 4 : ye * 4, x * 4 : xe * 4] = (
                        v.float()
                        .clamp_(0, 1)
                        .mul_(255)
                        .round_()
                        .byte()
                        .permute(1, 2, 0)
                        .cpu()
                        .numpy()
                    )
                    del t, v
        result = Image.fromarray(out)
        return result if color else result.convert("L")

    def flat_upscale(self, image, model, rule, color):
        import sys, gc, torch
        from pathlib import Path
        from PIL import Image

        root = model["nunif_root"]
        if root not in sys.path:
            sys.path.insert(0, root)
        import waifu2x.models
        from waifu2x.utils import Waifu2x

        noise = int(rule.get("noise_level", -1))
        method = "noise_scale4x" if noise >= 0 else "scale4x"
        key = (model["path"], "waifu2x", method, noise)
        if self.active != key:
            self.model = None
            gc.collect()
            torch.cuda.empty_cache()
            self.model = Waifu2x(str(Path(model["path"]).parent), [0])
            self.model.load_model(method, noise)
            self.active = key
        check()
        x = torch.from_numpy(np.asarray(image.convert("RGB"), dtype=np.float32) / 255).permute(
            2, 0, 1
        )
        with torch.inference_mode():
            result, _ = self.model.convert(
                x,
                None,
                method,
                noise,
                tile_size=rule.get("tile", 256),
                batch_size=1,
                tta=False,
                enable_amp=True,
            )
        check()
        pixels = result.clamp(0, 1).mul(255).round().byte().permute(1, 2, 0).cpu().numpy()
        im = Image.fromarray(pixels)
        return im if color else im.convert("L")

    def trt_upscale(self, image, model, rule, color):
        import gc, torch
        from pathlib import Path
        from janai_trt import Engine as TensorEngine
        from pipeline import write

        emit = getattr(self, "emit", lambda event: None)
        if self.trt is None:
            self.model = None
            self.active = None
            gc.collect()
            torch.cuda.empty_cache()
            self.trt = TensorEngine(
                Path(self.resource_root)
                if self.resource_root
                else Path(__file__).resolve().parent.parent,
                emit,
            )
        check()
        return self.trt.upscale(image, model, rule, color)
