"""Process services configured by the entry point, without importing __main__."""

from asyncio import Lock
from weakref import WeakValueDictionary

manager = None
scheduler = None
master_app = None
_operation_locks = WeakValueDictionary()


def operation_lock(key):
    lock = _operation_locks.get(key)
    if lock is None:
        lock = Lock()
        _operation_locks[key] = lock
    return lock


def configure(*, bot_manager=None, bot_scheduler=None, application=None):
    global manager, scheduler, master_app
    manager, scheduler, master_app = bot_manager, bot_scheduler, application
