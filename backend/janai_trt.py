"""TensorRT tiled inference with reusable engine caches."""

from control import check
from pathlib import Path
import hashlib, json, sys, time
import numpy as np
import torch
import tensorrt as trt


class Engine:
    def __init__(self, skill, emit):
        self.skill = Path(skill)
        self.emit = emit
        self.active = None
        self.ctx = None
        self.engine = None
        self.runtime = None
        self.stream = torch.cuda.Stream()
        torch.set_num_threads(4)

    def prepare(self, key, model, core, pad):
        self.scale = int(model.get("scale", 4))
        shape = core + 2 * pad
        onnxpath = Path(model.get("onnx", model["path"]))
        if onnxpath.suffix.lower() != ".onnx":
            onnxpath = self.skill / "runtime/onnx" / (key + ".onnx")
            onnxpath.parent.mkdir(parents=True, exist_ok=True)
            if not onnxpath.exists():
                self.emit({"state": "exporting", "model": key})
                from spandrel import ModelLoader

                desc = ModelLoader().load_from_file(model["path"]).eval().cpu().float()
                temp_onnx = onnxpath.with_name(onnxpath.name + ".building")
                torch.onnx.export(
                    desc.model,
                    torch.zeros(1, 3, 64, 64),
                    str(temp_onnx),
                    input_names=["input"],
                    output_names=["output"],
                    opset_version=17,
                    dynamic_axes={
                        "input": {2: "height", 3: "width"},
                        "output": {2: "out_height", 3: "out_width"},
                    },
                    dynamo=False,
                )
                temp_onnx.replace(onnxpath)
                del desc
        h = hashlib.sha256(onnxpath.read_bytes()).hexdigest()[:16]
        gpu = torch.cuda.get_device_properties(0)
        cache = self.skill / "runtime/engines"
        cache.mkdir(parents=True, exist_ok=True)
        policy = "norm32-v3" if model.get("scale", 4) == 2 else "norm32-v2"
        enginefile = (
            cache
            / f"{key}-{h}-sm{gpu.major}{gpu.minor}-trt{trt.__version__}-{shape}-fp16-{policy}.engine"
        )
        cache_hit = enginefile.exists()
        logger = trt.Logger(trt.Logger.ERROR)
        if not enginefile.exists():
            self.emit(
                {
                    "state": "preparing-engine",
                    "model": key,
                    "tile": shape,
                    "reason": "no_completed_engine",
                }
            )
            builder = trt.Builder(logger)
            network = builder.create_network(
                1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)
            )
            parser = trt.OnnxParser(network, logger)
            if not parser.parse(onnxpath.read_bytes()):
                for i in range(parser.num_errors):
                    print(parser.get_error(i), file=sys.stderr)
                raise RuntimeError("onnx_parse_failed")
            config = builder.create_builder_config()
            config.set_flag(trt.BuilderFlag.FP16)
            config.set_flag(trt.BuilderFlag.OBEY_PRECISION_CONSTRAINTS)
            # FDAT's normalization epsilon is ~1e-12: retain FP32 throughout sensitive operations.
            for index in range(network.num_layers):
                layer = network.get_layer(index)
                is_division = False
                if layer.type == trt.LayerType.ELEMENTWISE:
                    # get_layer returns the base ILayer wrapper in TensorRT 10.9.
                    layer.__class__ = trt.IElementWiseLayer
                    is_division = layer.op == trt.ElementWiseOperation.DIV
                sensitive = (
                    layer.type
                    in (trt.LayerType.NORMALIZATION, trt.LayerType.REDUCE, trt.LayerType.SOFTMAX)
                    or "clamp_min" in layer.name
                    or "Clip" in layer.name
                    or is_division
                )
                has_float_input = any(
                    layer.get_input(i) is not None
                    and layer.get_input(i).dtype in (trt.float32, trt.float16, trt.bfloat16)
                    for i in range(layer.num_inputs)
                )
                if sensitive and has_float_input:
                    layer.precision = trt.float32
                    for output_index in range(layer.num_outputs):
                        tensor = layer.get_output(output_index)
                        if tensor is not None and tensor.dtype in (
                            trt.float32,
                            trt.float16,
                            trt.bfloat16,
                        ):
                            layer.set_output_type(output_index, trt.float32)
            config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1024**3)
            config.builder_optimization_level = 3
            profile = builder.create_optimization_profile()
            name = network.get_input(0).name
            requested = (1, 3, shape, shape)
            # TensorRT 10.9 set_shape succeeds with return None; verify stored state instead.
            profile.set_shape(name, requested, requested, requested)
            if tuple(tuple(v) for v in profile.get_shape(name)) != (
                requested,
                requested,
                requested,
            ):
                raise RuntimeError("trt_profile_failed")
            config.add_optimization_profile(profile)
            self.emit(
                {
                    "state": "building-engine",
                    "model": key,
                    "tile": shape,
                    "reason": "no_completed_engine",
                }
            )
            serialized = builder.build_serialized_network(network, config)
            if serialized is None:
                raise RuntimeError("trt_build_failed")
            temp = enginefile.with_suffix(".building")
            temp.write_bytes(bytes(serialized))
            temp.replace(enginefile)
            self.emit({"state": "engine-built", "model": key, "bytes": enginefile.stat().st_size})
            del parser, network, config, profile, builder, serialized
        self.runtime = trt.Runtime(logger)
        self.engine = self.runtime.deserialize_cuda_engine(enginefile.read_bytes())
        if self.engine is None:
            raise RuntimeError("trt_load_failed")
        self.ctx = self.engine.create_execution_context()
        if self.ctx is None:
            raise RuntimeError("trt_context_failed")
        self.input_name = next(
            self.engine.get_tensor_name(i)
            for i in range(self.engine.num_io_tensors)
            if self.engine.get_tensor_mode(self.engine.get_tensor_name(i)) == trt.TensorIOMode.INPUT
        )
        self.output_name = next(
            self.engine.get_tensor_name(i)
            for i in range(self.engine.num_io_tensors)
            if self.engine.get_tensor_mode(self.engine.get_tensor_name(i))
            == trt.TensorIOMode.OUTPUT
        )
        result = self.ctx.set_input_shape(self.input_name, (1, 3, shape, shape))
        if result is False:
            raise RuntimeError("trt_input_shape_failed")
        dtypes = {
            trt.float32: torch.float32,
            trt.float16: torch.float16,
            trt.bfloat16: torch.bfloat16,
        }
        self.input = torch.empty(
            (1, 3, shape, shape),
            device="cuda",
            dtype=dtypes[self.engine.get_tensor_dtype(self.input_name)],
        )
        outshape = tuple(self.ctx.get_tensor_shape(self.output_name))
        if outshape != (1, 3, shape * self.scale, shape * self.scale):
            raise RuntimeError("unexpected_trt_shape")
        self.output = torch.empty(
            outshape, device="cuda", dtype=dtypes[self.engine.get_tensor_dtype(self.output_name)]
        )
        if not self.ctx.set_tensor_address(
            self.input_name, self.input.data_ptr()
        ) or not self.ctx.set_tensor_address(self.output_name, self.output.data_ptr()):
            raise RuntimeError("trt_binding_failed")
        if cache_hit:
            self.emit({"state": "engine-cache-hit", "model": key, "tile": shape})
        self.active = (key, core, pad)

    def upscale(self, image, model, rule, color):
        from PIL import Image

        key = rule["model"]
        core = int(rule.get("tile", 256 if color else 384))
        pad = int(rule.get("padding", 64 if color else 96))
        if self.active != (key, core, pad):
            self.ctx = None
            self.engine = None
            self.runtime = None
            if hasattr(self, "input"):
                del self.input, self.output
            torch.cuda.empty_cache()
            self.prepare(key, model, core, pad)
        a = np.asarray(image.convert("RGB"), dtype=np.float32) / 255
        h, w = a.shape[:2]
        out = np.empty((h * self.scale, w * self.scale, 3), dtype=np.uint8)
        fixed = core + 2 * pad
        with torch.inference_mode():
            for y in range(0, h, core):
                for x in range(0, w, core):
                    check()
                    ye, xe = min(y + core, h), min(x + core, w)
                    y0, x0 = max(y - pad, 0), max(x - pad, 0)
                    y1, x1 = min(ye + pad, h), min(xe + pad, w)
                    tile = a[y0:y1, x0:x1]
                    # Preserve true image-boundary context; only pad unused tile tail.
                    tile = np.pad(
                        tile,
                        ((0, fixed - tile.shape[0]), (0, fixed - tile.shape[1]), (0, 0)),
                        mode="edge",
                    )
                    with torch.cuda.stream(self.stream):
                        self.input.copy_(
                            torch.from_numpy(tile.copy()).permute(2, 0, 1).unsqueeze(0)
                        )
                        if not self.ctx.execute_async_v3(self.stream.cuda_stream):
                            raise RuntimeError("trt_execute_failed")
                    self.stream.synchronize()
                    crop = self.output[
                        0,
                        :,
                        (y - y0) * self.scale : (ye - y0) * self.scale,
                        (x - x0) * self.scale : (xe - x0) * self.scale,
                    ]
                    if not torch.isfinite(crop).all():
                        raise RuntimeError("nonfinite_output")
                    out[y * self.scale : ye * self.scale, x * self.scale : xe * self.scale] = (
                        crop.float()
                        .clamp(0, 1)
                        .mul(255)
                        .round()
                        .byte()
                        .permute(1, 2, 0)
                        .cpu()
                        .numpy()
                    )
        result = Image.fromarray(out)
        return result if color else result.convert("L")
