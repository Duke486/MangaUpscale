import argparse, ctypes, os, sys, json, traceback
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from pipeline import run, write, read, error_report


def main():
    from parent_guard import start as guard_parent

    guard_parent()
    p = argparse.ArgumentParser()
    p.add_argument("--job", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--runtime", required=True)
    p.add_argument("--wait-pid", type=int, action="append", default=[])
    a = p.parse_args()
    job = Path(a.job)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    lock = None
    acquired = False
    try:
        if a.wait_pid:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.WaitForSingleObject.restype = ctypes.c_uint32
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            # Hold handles from launch, so PID reuse cannot change who we wait for.
            handles = []
            try:
                for pid in a.wait_pid:
                    h = kernel.OpenProcess(0x00100000, False, pid)
                    if not h and ctypes.get_last_error() != 87:
                        raise OSError("predecessor_access_failed")
                    if h:
                        handles.append(h)
                write(
                    job / "status/status.json",
                    {"state": "waiting", "pid": os.getpid(), "predecessors": a.wait_pid},
                )
                for h in handles:
                    while True:
                        v = kernel.WaitForSingleObject(h, 10000)
                        if v == 0:
                            break
                        if v != 258:
                            raise OSError("wait_failed")
            finally:
                for h in handles:
                    kernel.CloseHandle(h)
        lock = kernel.CreateMutexW(None, False, "Local\\MangaUpscaleSingleGPU")
        if not lock:
            raise OSError("gpu_mutex_create_failed")
        if kernel.WaitForSingleObject(lock, 0) not in (0, 128):
            raise RuntimeError("another_janai_task_is_running")
        acquired = True
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            run(job, a.plan, a.runtime)
        try:
            from cleanup_completed import cleanup

            result = cleanup(job)
            current = read(job / "status/status.json")
            current.update(result)
            write(job / "status/status.json", current)
        except BaseException as e:
            current = read(job / "status/status.json")
            current["cache_cleanup"] = "incomplete"
            current["cleanup_error_type"] = type(e).__name__
            write(job / "status/status.json", current)
    except BaseException as e:
        write(job / "status/status.json", error_report(e))
        sys.exit(1)
    finally:
        if acquired:
            kernel.ReleaseMutex(lock)
        if lock:
            kernel.CloseHandle(lock)


if __name__ == "__main__":
    main()
