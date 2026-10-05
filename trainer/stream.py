"""Resource and prefetch handling for long-running training jobs."""

import queue
import threading


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
