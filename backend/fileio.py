"""Windows readers allow atomic replacement; durable checkpoint writes retry locks."""

import json, os, time, uuid
from pathlib import Path


def read_text_shared(path):
    if os.name != "nt":
        return Path(path).read_text(encoding="utf-8-sig")
    import ctypes, msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(Path(path).absolute()), 0x80000000, 0x7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, "r", encoding="utf-8-sig") as stream:
        return stream.read()


def atomic_json(path, value, optional=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + str(os.getpid()) + "-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            if not optional:
                os.fsync(stream.fileno())
        for attempt in range(12):
            try:
                temporary.replace(path)
                return True
            except PermissionError:
                if attempt == 11:
                    if optional:
                        return False
                    raise
                time.sleep(min(0.01 * 2**attempt, 0.15))
    except OSError:
        if optional:
            return False
        raise
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
