"""Sheeter: read the notes off a photo of sheet music."""

__version__ = "1.0.0"


def lazy_import(name):
    """*name*, loaded the first time something on it is used rather than now.

    scipy.ndimage is a few tens of MB once imported, and a server that is mostly
    idle should not pay for it until somebody actually uploads a page.
    """
    import importlib.util
    import sys

    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.find_spec(name)
    loader = importlib.util.LazyLoader(spec.loader)
    spec.loader = loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module
