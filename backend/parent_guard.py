"""Stop a child when its owning process exits; retain disk checkpoints."""

import os, ctypes, threading


def start():
    if os.name != "nt":
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    owners = {int(os.environ.get("MANGA_OWNER_PID", os.getppid())), os.getppid()}
    os.environ["MANGA_OWNER_PID"] = str(os.getpid())
    for owner in owners:
        handle = kernel.OpenProcess(0x00100000, False, owner)
        if not handle:
            raise RuntimeError("owner_process_unavailable")

        def watch(h=handle):
            result = kernel.WaitForSingleObject(h, 0xFFFFFFFF)
            kernel.CloseHandle(h)
            if result == 0:
                os._exit(3)

        threading.Thread(target=watch, daemon=True).start()
