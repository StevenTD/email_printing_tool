import hashlib
import os
import threading
from contextlib import contextmanager


_local_locks = {}
_local_locks_guard = threading.Lock()


@contextmanager
def printer_mutex(printer_name):
    mutex_name = hashlib.sha256(printer_name.casefold().encode("utf-8")).hexdigest()
    if os.name != "nt":
        with _local_locks_guard:
            lock = _local_locks.setdefault(mutex_name, threading.Lock())
        with lock:
            yield
        return

    try:
        import win32api
        import win32event
    except ImportError as exc:
        raise RuntimeError("pywin32 is required for cross-process printer queue locking.") from exc

    handle = win32event.CreateMutex(None, False, f"Local\\MailPrint-{mutex_name}")
    acquired = False
    try:
        result = win32event.WaitForSingleObject(handle, win32event.INFINITE)
        if result not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
            raise RuntimeError(f"Could not acquire the mutex for printer {printer_name!r}.")
        acquired = True
        yield
    finally:
        if acquired:
            win32event.ReleaseMutex(handle)
        win32api.CloseHandle(handle)
