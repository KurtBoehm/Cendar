# sane.pyi
#
# Stub file for sane.py – suitable for use with pyright / mypy.
from __future__ import annotations

from typing import Any, Callable, Iterator, Sequence

import numpy as np
from PIL.Image import Image

__version__: str
__author__: list[str]

# These are dictionaries mapping constants from _sane to strings.
TYPE_STR: dict[int, str]
UNIT_STR: dict[int, str]

class Option:
    """
    Class representing a SANE option.
    """

    # args is what comes from dev.get_options(); scanDev is a SaneDev
    def __init__(self, args: Sequence[Any], scanDev: SaneDev) -> None: ...

    scanDev: SaneDev
    index: int
    name: Any
    title: str
    desc: str
    type: int
    unit: int
    size: int
    cap: int
    constraint: Any
    py_name: str

    def is_active(self) -> bool: ...
    def is_settable(self) -> bool: ...
    def __repr__(self) -> str: ...

class _SaneIterator(Iterator[Image]):
    """
    Iterator for ADF scans.
    """

    def __init__(self, device: SaneDev) -> None: ...
    device: SaneDev

    def __iter__(self) -> _SaneIterator: ...
    def __next__(self) -> Image: ...
    def __del__(self) -> None: ...

DeviceSignature = tuple[str, str, str, str]
ScannerModel = tuple[str, str]
ScanArea = tuple[tuple[float, float], tuple[float, float]]

# (format, last_frame, (pixels_per_line, lines), depth, bytes_per_line)
ScanParameters = tuple[str, bool, tuple[int, int], int, int]

_Progress = Callable[[int, int], None]

class _SaneRawDevice:
    """
    Typing stub for the underlying _sane device object returned by _sane._open.
    """

    # Options
    def get_options(self) -> list[Sequence[Any]]: ...
    def get_option(self, index: int) -> Any: ...
    def set_option(self, index: int, value: Any) -> int: ...

    # Scanning
    def get_parameters(self) -> ScanParameters: ...
    def start(self) -> None: ...
    def cancel(self) -> None: ...

    # snap(no_cancel, as_array, progress)
    # returns: (data, width, height, samples, sampleSize)
    def snap(
        self, no_cancel: bool, as_array: bool, progress: _Progress | None = None
    ) -> tuple[bytes | bytearray | memoryview, int, int, int, int]: ...

    # File descriptor and closing
    def fileno(self) -> int: ...
    def close(self) -> None: ...

class SaneDev:
    """
    Class representing a SANE device.
    """

    devname: str
    dev: _SaneRawDevice
    opt: dict[str, Option]

    # "virtual" / computed attributes (resolved via __getattr__):
    #   optlist: list of option names
    #   area: ((tl_x, tl_y), (br_x, br_y))
    #   sane_signature: (devname, brand, name, type)
    #   scanner_model: (brand, name)

    def __init__(self, devname: str) -> None: ...

    # Context manager
    def __enter__(self) -> "SaneDev": ...
    def __exit__(self, *args: Any) -> None: ...

    # Dynamic attribute access (__getattr__/__setattr__) is used to expose
    # options. Stubs deliberately leave these as Any to avoid over-constraining.
    def __getattr__(self, key: str) -> Any: ...
    def __setattr__(self, key: str, value: Any) -> None: ...
    def __getitem__(self, key: str) -> Option: ...
    def get_parameters(self) -> ScanParameters: ...
    def get_options(self) -> list[Sequence[Any]]: ...
    def start(self) -> None: ...
    def cancel(self) -> None: ...
    def snap(
        self, no_cancel: bool = False, progress: _Progress | None = None
    ) -> Image: ...
    def scan(self, progress: _Progress | None = None) -> Image: ...
    def arr_snap(self, progress: _Progress | None = None) -> np.ndarray: ...
    def arr_scan(self, progress: _Progress | None = None) -> np.ndarray: ...
    def multi_scan(self) -> _SaneIterator: ...
    def fileno(self) -> int: ...
    def close(self) -> None: ...

def init() -> tuple[int, int, int, int]: ...

# sane_ver, ver_maj, ver_min, ver_patch

def get_devices(
    localOnly: bool = False,
) -> list[DeviceSignature]: ...
def open(devname: str) -> SaneDev: ...
def exit() -> None: ...
