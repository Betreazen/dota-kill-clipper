"""OS-backed locks, released even if the owning process exits unexpectedly."""
import os
import time
from pathlib import Path


class FileLock:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open('a+b')
        if file.seek(0, 2) == 0:
            file.write(b'0')
            file.flush()
        file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            file.close()
            return False
        self.file = file
        return True

    def release(self):
        if self.file is not None:
            self.file.close()
            self.file = None

    def __enter__(self):
        deadline = time.monotonic() + 10
        while not self.acquire():
            if time.monotonic() >= deadline:
                raise TimeoutError(f'File busy: {self.path.name}')
            time.sleep(0.02)
        return self

    def __exit__(self, *args):
        self.release()
