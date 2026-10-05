"""Resource and prefetch handling for long-running training jobs."""

import queue
import threading


def frozen_record_count(actual_bytes, frozen_bytes=None, record_bytes=32):
    """Allow source growth, reject a shortened frozen prefix, ignore tail bytes."""
    if actual_bytes < 0 or record_bytes < 1 or (frozen_bytes is not None and frozen_bytes < 0):
        raise ValueError("invalid frozen input length")
    if frozen_bytes is not None and actual_bytes < frozen_bytes:
        raise RuntimeError("frozen input shortened before training")
    return (actual_bytes if frozen_bytes is None else frozen_bytes) // record_bytes


def complete_batches(buffers, batch_size, concatenate):
    """Carry buffer tails forward; yield every row once, with one final tail.

    Full batches keep optimizer step counts independent of buffer boundaries.
    The adapter keeps NumPy optional for lightweight controller tests.
    """
    if batch_size < 1:
        raise ValueError("batch size must be positive")
    tail = None
    for buffer in buffers:
        if not len(buffer):
            continue
        if tail is not None:
            buffer = concatenate([tail, buffer])
        end = len(buffer) // batch_size * batch_size
        for start in range(0, end, batch_size):
            yield buffer[start:start + batch_size]
        tail = buffer[end:] if end < len(buffer) else None
    if tail is not None:
        yield tail


def prepare_file_limit(file_count):
    # NumPy's memory maps retain descriptors on macOS. launchd starts with
    # a much smaller soft limit than an interactive shell.
    try:
        import resource
    except ImportError:  # Windows has no POSIX descriptor limit.
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    needed = file_count + 128
    if soft == resource.RLIM_INFINITY or soft >= needed:
        return
    if hard != resource.RLIM_INFINITY and hard < needed:
        raise RuntimeError(f"training needs {needed} open files; the process hard limit is {hard}")
    resource.setrlimit(resource.RLIMIT_NOFILE, (needed, hard))


def prefetch(gen, depth=6):
    """Bound decoded batches and deliver producer failures to the trainer."""
    q = queue.Queue(maxsize=depth)
    stopped = threading.Event()

    def put(kind, item=None):
        while not stopped.is_set():
            try:
                q.put((kind, item), timeout=0.1)
                return
            except queue.Full:
                pass

    def run():
        try:
            for item in gen:
                if stopped.is_set():
                    break
                put("item", item)
        except BaseException as error:
            put("error", error)
        finally:
            close = getattr(gen, "close", None)
            try:
                if close:
                    close()
            except BaseException as error:
                put("error", error)
            finally:
                put("done")

    worker = threading.Thread(target=run, name="training-prefetch", daemon=True)
    worker.start()
    try:
        while True:
            kind, item = q.get()
            if kind == "done":
                return
            if kind == "error":
                raise item
            yield item
    finally:
        stopped.set()
        worker.join(timeout=5)
