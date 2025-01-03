from multiprocessing.managers import SharedMemoryManager
from multiprocessing.shared_memory import SharedMemory


class SHM:
    smm = SharedMemoryManager()
    mmap: dict[str, SharedMemory] = {}
    is_start = False

    @classmethod
    def start(cls):
        if cls.is_start:
            return
        cls.smm.start()
        cls.is_start = True

    @classmethod
    def create(cls, size=1024):
        cls.start()
        sm = cls.smm.SharedMemory(size)
        cls.mmap[sm.name] = sm
        return sm

    @classmethod
    def create2(cls, name, size=1024):
        return SharedMemory(name=name, size=size, create=True)

    @classmethod
    def erase(cls, name):
        sm = cls.mmap.pop(name, None)
        if not sm:
            return
        sm.close()
        sm.unlink()

    @classmethod
    def clear(cls):
        for name in list(cls.mmap):
            cls.erase(name)
