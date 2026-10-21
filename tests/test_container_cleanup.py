"""0.5.4: forgetting a garbage-collected container never blocks and never forgets another one.

A container's class list is dropped by a weakref finalizer. The garbage
collector may run it in any thread, at any allocation - also while another
thread holds the library lock (and perhaps waits for this thread). It must not
wait for that lock. And since ``id()`` values are reused, a late finalizer must
not drop the list of a NEW container that got the same id: validation would
then see no class at all and pass silently.
"""

import gc
import threading
import time
import weakref

from py_autowired import Container, autowired


def _tracked_container():
    container = Container()

    class Consumer:
        missing_instance = None

    autowired._remember_patched_class(container, Consumer)
    return container, Consumer


def test_collecting_a_container_does_not_wait_for_a_busy_lock():
    container, _consumer = _tracked_container()
    key = id(container)
    held, release = threading.Event(), threading.Event()

    def hold():
        with autowired._GLOBAL_LOCK:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)
    timer = threading.Timer(2.0, release.set)                 # the holder lets go after 2 s
    timer.start()
    try:
        started = time.perf_counter()
        del container
        gc.collect()                                          # runs the finalizer here
        waited = time.perf_counter() - started
    finally:
        release.set()
        timer.cancel()
        holder.join(5)
    assert waited < 1.0, f"the finalizer waited {waited:.2f}s for the lock"
    autowired.refresh()                                       # the next lock holder catches up
    autowired._remember_patched_class(Container(), type("Other", (), {}))
    assert key not in autowired._PATCHED_CLASSES or not autowired._PATCHED_CLASSES[key]


def test_a_late_finalizer_does_not_forget_a_new_container_with_the_same_id():
    old, _consumer = _tracked_container()
    key = id(old)
    newer = weakref.WeakValueDictionary()                     # what a new container with that id owns
    with autowired._GLOBAL_LOCK:
        autowired._PATCHED_CLASSES[key] = newer
    del old
    gc.collect()                                              # the old container's finalizer runs late
    try:
        assert autowired._PATCHED_CLASSES.get(key) is newer
    finally:
        with autowired._GLOBAL_LOCK:
            if autowired._PATCHED_CLASSES.get(key) is newer:
                del autowired._PATCHED_CLASSES[key]
