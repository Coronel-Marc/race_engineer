"""Legacy direct view of Assetto Corsa shared memory.

New code should use :mod:`raw_telemetry_reader`, which copies data into
immutable Python values and owns its mappings explicitly.
"""
import ctypes
import mmap

try:  # Support both ``import sim_info`` and ``import src.sim_info``.
    from .ac_shared_memory import *  # noqa: F401,F403
    from .ac_shared_memory import (
        SPageFilePhysics, SPageFileGraphic, SPageFileStatic,
    )
except ImportError:
    from ac_shared_memory import *  # noqa: F401,F403
    from ac_shared_memory import (
        SPageFilePhysics, SPageFileGraphic, SPageFileStatic,
    )


class SimInfo:
    """Compatibility wrapper exposing the three live ctypes structures."""

    def __init__(self):
        self._acpmf_physics = None
        self._acpmf_graphics = None
        self._acpmf_static = None
        self.physics = None
        self.graphics = None
        self.static = None
        try:
            self._acpmf_physics = mmap.mmap(
                0, ctypes.sizeof(SPageFilePhysics), 'acpmf_physics'
            )
            self._acpmf_graphics = mmap.mmap(
                0, ctypes.sizeof(SPageFileGraphic), 'acpmf_graphics'
            )
            self._acpmf_static = mmap.mmap(
                0, ctypes.sizeof(SPageFileStatic), 'acpmf_static'
            )
            self.physics = SPageFilePhysics.from_buffer(self._acpmf_physics)
            self.graphics = SPageFileGraphic.from_buffer(self._acpmf_graphics)
            self.static = SPageFileStatic.from_buffer(self._acpmf_static)
        except Exception:
            self.close()
            raise

    def close(self):
        # Release exported buffer views before closing their mmaps.
        self.physics = self.graphics = self.static = None
        for name in ('_acpmf_physics', '_acpmf_graphics', '_acpmf_static'):
            mapping = getattr(self, name)
            if mapping is not None:
                mapping.close()
                setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


info = SimInfo()


def demo():
    import time

    for _ in range(400):
        print(info.static.track, info.graphics.tyreCompound, info.graphics.currentTime,
              info.physics.rpms, info.graphics.currentTime, info.static.maxRpm,
              list(info.physics.tyreWear))
        time.sleep(0.1)


def do_test():
    for struct in info.static, info.graphics, info.physics:
        print(struct.__class__.__name__)
        for field, _type_spec in struct._fields_:
            value = getattr(struct, field)
            if not isinstance(value, (str, float, int)):
                value = list(value)
            print(" {} -> {} {}".format(field, type(value), value))


if __name__ == '__main__':
    do_test()
    demo()
