"""Desktop job lifecycle and progress reporting."""

import hashlib, json, os, subprocess, sys, time
from pathlib import Path
from backend.fileio import read_text_shared, atomic_json
from backend.event_stream import read_events
from backend.runtime_config import load_runtime


ROOT = Path(__file__).resolve().parent
messagebox = None  # The Qt frontend installs its dialog adapter at startup.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
STATES = {
    "restarting-bw": "重新处理黑白页，彩页缓存保留",
    "skipped-user": "已跳过并清理超分缓存",
    "restarting-book": "从头重做当前漫画",
    "page-inference": "正在超分",
    "page-submitted": "本页推理结束",
    "page-complete": "本页已保存",
    "preparing-engine": "准备彩页加速引擎",
    "building-engine": "首次编译彩页加速引擎",
    "engine-built": "彩页加速引擎已保存",
    "engine-cache-hit": "复用彩页加速引擎",
    "repack95-encoding": "以质量95重新编码",
    "repack95-verifying": "校验重打包文件",
    "repack95-complete": "质量95成品已安全替换",
    "repack95-skipped": "该成品已按质量95重打包，跳过",
    "skipped-existing": "目标成品已存在，跳过",
    "copying": "下载本地缓存",
    "retrying_source": "等待网络重试",
    "scanning": "检查页面",
    "scanned": "本包分析完成",
    "running": "正在超分",
    "model-preparing": "正在处理本页",
    "packaging-jpeg": "编码 JPG 并打包",
    "verifying": "校验成品",
    "complete": "本包完成",
    "prepared": "缓存准备完成",
    "reported": "检测完成",
    "finished": "任务完成",
    "finished_with_errors": "结束，有检测失败",
    "failed": "任务失败",
    "waiting": "等待前序任务",
    "checking-source": "确认源文件状态",
    "checking-prefix": "核对已下载部分",
    "hashing-local": "校验本地下载缓存",
    "hashing-stage": "检查本地缓存",
    "analysing-pages": "本地逐页分析",
    "stopped": "已停止，断点已保留",
    "download-verified": "下载完整性验证通过",
    "download-cache-verified": "完整下载缓存校验通过，直接复用",
    "cache-verified": "页面缓存校验通过，直接复用",
    "keep-original": "本页分辨率足够，保留原尺寸",
    "retrying-book": "临时失败，正在重试",
    "book-failed-continue": "本包失败，继续后续包",
    "prepared-reused": "复用已准备任务，继续处理",
}


def read_json(path):
    try:
        value = json.loads(read_text_shared(path))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def task_path(source, output, cache, threshold, samples):
    values = [
        "v5-original-names",
        os.path.normcase(os.path.abspath(source)),
        os.path.normcase(os.path.abspath(output)),
        threshold,
        samples,
    ]
    return Path(cache) / ("job-gui-" + hashlib.sha256(json.dumps(values).encode()).hexdigest()[:20])


class Controller:
    def start(self, run, output_conflicts=None, *, trial=False, book_keys=()):
        if self.process and self.process.poll() is None:
            return
        try:
            threshold = float(self.threshold.get())
            samples = int(self.samples.get())
            if not 1 <= threshold <= 100 or not 1 <= samples <= 10:
                raise ValueError()
            source = self.source.get().strip()
            output = self.output.get().strip()
            cache = self.cache.get().strip()
            if not source or not output or not cache:
                raise ValueError()
        except ValueError:
            messagebox.showerror("参数错误", "请填写路径，倍率需为 1–100，抽检页数需为 1–10。")
            return
        runtime = load_runtime(ROOT / "runtime.json")
        if not runtime or not Path(runtime.get("python", "")).is_file():
            messagebox.showerror("运行环境不可用", "runtime.json 中配置的 Python 路径不存在。")
            return
        self.job = (
            Path(self.existing.get().strip())
            if self.existing.get().strip()
            else task_path(source, output, cache, threshold, samples)
        )
        do_repack = False
        if do_repack and not (self.job / "state/demand.json").exists():
            messagebox.showerror("需要已有任务", "请先填写要重打包的已有任务目录。")
            return
        try:
            lock_path = self.job / "controller/state/session.lock"
            if os.name == "nt" and lock_path.exists():
                import msvcrt

                try:
                    with lock_path.open("r+b") as lockfile:
                        msvcrt.locking(lockfile.fileno(), msvcrt.LK_NBLCK, 1)
                        lockfile.seek(0)
                        msvcrt.locking(lockfile.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError as e:
                    messagebox.showerror(
                        "无法取得任务锁",
                        f"任务可能仍在其他窗口运行，或当前窗口没有缓存目录的访问权限。请关闭此窗口，从桌面重新打开程序后继续。系统错误码：{e.errno}。",
                    )
                    return
            (self.job / "status").mkdir(parents=True, exist_ok=True)
            settings = {
                "source": source,
                "output": output,
                "cache": cache,
                "existing": self.existing.get().strip(),
                "threshold": threshold,
                "samples": samples,
                "bw_mode": self.bw_mode.get(),
                "skip_large": self.skip_large.get(),
                "page_height": self.threshold_scale.value(),
            }
            atomic_json(ROOT / "gui-settings.json", settings)
            (self.job / "status/stop.request").unlink(missing_ok=True)
            (self.job / "status/pause.request").unlink(missing_ok=True)
            args = [
                runtime["python"],
                "-B",
                "-u",
                str(ROOT / "backend/demand.py"),
                "--input",
                source,
                "--job",
                str(self.job),
                "--output-root",
                output,
                "--threshold",
                str(threshold),
                "--samples",
                str(samples),
                "--bw-mode",
                {"平涂": "flat", "强制全局彩页": "color"}.get(self.bw_mode.get(), "manga"),
                "--run" if run else "--report-only",
            ]
            args.extend(["--page-height", str(self.threshold_scale.value())])
            if output_conflicts is not None:
                args.extend(["--output-conflicts", output_conflicts])
            if not self.skip_large.get():
                args.append("--allow-above-recommended")
            for key in book_keys:
                args.extend(["--book-key", key])
            if do_repack:
                args.append("--repack95")
            self.log_stream = (self.job / "status/gui-controller.log").open("ab", buffering=0)
            self.started_at = time.time()
            event_file = self.job / "status/events.jsonl"
            self.event_offset = event_file.stat().st_size if event_file.exists() else 0
            self.event_previous = None
            self.process = subprocess.Popen(
                args,
                stdout=self.log_stream,
                stderr=self.log_stream,
                cwd=ROOT,
                creationflags=CREATE_NO_WINDOW,
                env={**os.environ, "MANGA_OWNER_PID": str(os.getpid())},
            )
            atomic_json(
                self.job / "status/gui-process.json", {"pid": self.process.pid}, optional=True
            )
        except OSError as e:
            if self.log_stream:
                self.log_stream.close()
                self.log_stream = None
            messagebox.showerror("启动失败", f"无法启动任务，系统错误码 {e.errno}。")
            return
        if do_repack:
            self.repack95.set(False)
        self.progress_book_key = None
        self.page_timings = {}
        self.open_trial_when_ready = trial
        self.run_book_keys = book_keys
        self.last_event = None
        self.busy(True)
        self.status.set("正在递归检测")
        self.detail.set("进度将自动保存")
        self.log(
            "已启动"
            + ("按需超分" if run else "只检测比例")
            + "，页高低于 "
            + str(self.threshold_scale.value())
            + " px。"
        )

    def poll_progress(self):
        parent = read_json(self.job / "status/status.json") or {}
        summary = (
            parent
            if parent.get("state") == "scanning"
            and "checked" in parent
            and parent.get("updated_epoch", 0) >= self.started_at
            else read_json(self.job / "status/demand-summary.json")
        )
        if summary:
            rate = summary.get("needs_upscale_percent")
            percent = "暂无比例" if rate is None else str(rate) + "%"
            self.summary.set(
                f"{'检测完成' if summary.get('scan_complete') else '已检测'} {summary['checked']} 包；需超分 {summary['selected']} 包（{percent}）；无需 {summary.get('not_needed', 0)} 包；失败 {summary['failed']} 包"
            )
        active = read_json(self.job / "status/active-task.json") or {}
        if active.get("updated_epoch", 0) < self.started_at:
            active = {}
        batch = read_json(self.job / "status/batch-progress.json") or {}
        complete = batch.get("task_completed", 0)
        child_count = batch.get("task_total", 0)
        available = (
            [(parent.get("updated_epoch", 0), parent)]
            if parent.get("updated_epoch", 0) >= self.started_at
            else []
        )
        if active.get("task"):
            active_report = read_json(self.job / "jobs" / active["task"] / "status/status.json")
            if active_report and active_report.get("updated_epoch", 0) >= self.started_at:
                available.append((active_report["updated_epoch"], active_report))
        else:
            available = [
                item for item in available if item[1].get("updated_epoch", 0) >= self.started_at
            ]
        if available:
            _, report = max(available, key=lambda item: item[0])
            state = report.get("state", "")
            text = STATES.get(state, state)
            if active.get("task") and report is not parent:
                if getattr(self, "progress_book_key", None) != active["task"]:
                    self.progress_book_key = active["task"]
                    manifest = read_json(self.job / "state/demand.json") or {}
                    book = manifest.get("items", {}).get(active["task"], {})
                    self.progress_book_name = (
                        Path(book["source"]).name if "source" in book else active["task"][:8]
                    )
                text = f"{active.get('index', 0)}/{active.get('total', 0)} · {self.progress_book_name} · {text}"
            done = report.get("completed")
            total = report.get("total")
            if done is not None and total is not None:
                text += f"  {done}/{total}"
            if report.get("bytes") is not None:
                text += f"  {report['bytes'] / 1048576:.1f}/{report.get('total_bytes', 1) / 1048576:.1f} MB"

            if report.get("code") == "unexpected_blank_page":
                text += f" · 第 {report['page']} 页疑似异常白页"
            self.status.set(text)
            age = max(0, time.time() - report.get("updated_epoch", time.time()))
            if age >= 10:
                self.status.set(text + f" · 已等待 {int(age)} 秒")
            if total and done is not None:
                self.progress.stop()
                self.progress.configure(mode="determinate", maximum=total, value=done)
            elif report.get("total_bytes"):
                self.progress.stop()
                self.progress.configure(
                    mode="determinate", maximum=report["total_bytes"], value=report.get("bytes", 0)
                )
            elif self.process and self.process.poll() is None:
                if str(self.progress["mode"]) != "indeterminate":
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(12)
            event = (state, done, total, report.get("archive"), report.get("code"))
            if not (self.job / "status/events.jsonl").exists() and event != self.last_event:
                self.log(STATES.get(state, state))
            self.last_event = event
            detail = f"已完成 {complete} / {child_count} 本"
            samples = getattr(self, "page_timings", {}).get(active.get("task"), [])
            if (
                state in ("page-inference", "page-submitted", "page-complete")
                and total
                and done is not None
                and len(samples) >= 5
            ):
                seconds = round((total - done) * sum(samples) / len(samples))
                if seconds > 0:
                    duration = (
                        f"{seconds // 60} 分 {seconds % 60} 秒"
                        if seconds >= 60
                        else f"{seconds} 秒"
                    )
                    detail += f" · 本包超分约剩 {duration}"
            self.detail.set(detail)

    def consume_events(self):
        path = self.job / "status/events.jsonl"
        if not path.exists():
            return
        events, self.event_offset = read_events(path, self.event_offset)
        for event in events:
            state = event.get("state", "")
            task = event.get("task", "")
            if task and state == "page-complete" and event.get("inference_s", 0) > 0:
                if not hasattr(self, "page_timings"):
                    self.page_timings = {}
                samples = self.page_timings.setdefault(task, [])
                samples.append(event["inference_s"] + event.get("decode_s", 0))
                del samples[:-12]
            key = (task, state)
            final = (
                event.get("total") is not None and event.get("completed") == event["total"]
            ) or (
                event.get("total_bytes") is not None and event.get("bytes") == event["total_bytes"]
            )
            if key == self.event_previous and not final:
                continue
            self.event_previous = key
            text = ("包 " + task[:8] + " · " if task else "") + STATES.get(state, state)
            if event.get("total_bytes") is not None:
                text += f" {event.get('bytes', 0) / 1048576:.1f}/{event['total_bytes'] / 1048576:.1f} MiB"
            elif event.get("total") is not None:
                text += f" {event.get('completed', 0)}/{event['total']}"
            if event.get("model"):
                text += " · " + event["model"]
            if event.get("code") == "unexpected_blank_page":
                text += f" · 第 {event['page']} 页疑似异常白页"
            elif event.get("code"):
                text += " " + event["code"]
            timing = [
                f"{label} {event[key]:.2f}s"
                for key, label in [
                    ("decode_s", "读取"),
                    ("inference_s", "推理"),
                    ("save_s", "保存校验"),
                ]
                if key in event
            ]
            if timing:
                text += " · " + " / ".join(timing)
            self.log(text, event.get("updated_epoch"))

    def tick(self):
        if self.closing:
            return
        if self.job and self.process:
            try:
                self.poll_progress()
                self.consume_events()
            except OSError:
                pass
        if (
            self.process
            and self.process.poll() is not None
            and not getattr(self, "stopping", False)
        ):
            code = self.process.returncode
            self.process = None
            self.busy(False)
            if self.log_stream:
                self.log_stream.close()
                self.log_stream = None
            failure = read_json(self.job / "status/status.json") or {}
            reason = (
                {
                    "source_directory_changed": "漫画目录与原任务不同",
                    "output_directory_changed": "输出目录与原任务不同",
                }.get(failure.get("code"), failure.get("code", "processing_failed"))
                + " / "
                + failure.get("error_type", "")
            )
            if failure.get("winerror") is not None:
                reason += " / Windows " + str(failure["winerror"])
            friendly = {
                "source_directory_changed": "漫画目录与原任务不同",
                "output_directory_changed": "保存位置与原任务不同",
                "insufficient_disk_space": "本地磁盘空间不足",
                "input_unavailable": "漫画目录暂时无法访问",
                "output_overlaps_input": "实际成品目录与输入目录重叠，请另选保存位置",
                "another_janai_task_is_running": "该任务正在其他窗口运行",
            }
            self.status.set(
                ("检测完成" if failure.get("state") == "reported" else "任务完成")
                if code == 0
                else (
                    "已停止，可继续"
                    if failure.get("state") == "stopped"
                    else friendly.get(failure.get("code"), "任务未完成，原因见下方日志")
                )
            )
            if code != 0:
                self.log(reason)
            if failure.get("name_conflicts_skipped"):
                self.log(f"已跳过 {failure['name_conflicts_skipped']} 个同目录重名文件")
            self.log("任务退出，退出码 " + str(code))
            if code != 0 and failure.get("code") == "output_name_collision":
                self.start(
                    True,
                    messagebox.output_conflicts(),
                    book_keys=getattr(self, "run_book_keys", ()),
                )
            if getattr(self, "open_trial_when_ready", False):
                self.open_trial_when_ready = False
                if code == 0 and failure.get("state") == "reported":
                    self.open_trial()
        if hasattr(self, "sync_pause"):
            self.sync_pause()
        self.root.after(1000, self.tick)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.status.set("正在停止，保存已有断点…")
            self.root.update_idletasks()
            try:
                if self.job:
                    (self.job / "status/stop.request").write_text("stop", encoding="ascii")
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
            forced = self.process.poll() is None
            result = (
                subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
                if forced
                else None
            )
            if result and result.returncode and self.process.poll() is None:
                self.log("停止失败，进程仍在运行。")
                return False
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.log("正在等待进程退出。")
                return False
            self.log(
                "已强制停止，当前未完成页或步骤将在续跑时重做。"
                if forced
                else "已在检查点停止，断点已保留。"
            )
        self.process = None
        self.busy(False)
        if self.log_stream:
            self.log_stream.close()
            self.log_stream = None
        self.status.set("已停止，可继续")
        return True

    def close(self):
        if not self.stop():
            return
        self.closing = True
        self.root.destroy()
