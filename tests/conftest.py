"""Test fixtures: generate small, valid PNG files without external deps."""

import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def write_png(path: Path, rgb: tuple[int, int, int], size: int = 8) -> None:
    """Write a solid-colour RGB PNG."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b""
    row = bytes(rgb) * size
    for _ in range(size):
        raw += b"\x00" + row  # filter byte + pixels

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


@pytest.fixture
def images_dir(tmp_path: Path) -> Path:
    d = tmp_path / "images"
    d.mkdir()
    write_png(d / "image001.png", (220, 40, 40))
    write_png(d / "image002.png", (40, 200, 40))
    write_png(d / "image003.png", (40, 40, 220))
    return d
