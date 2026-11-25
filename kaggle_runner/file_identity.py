"""Stable local file identities for Runner ledger and audit binding checks.

Windows ``os.stat`` metadata has changed representation across Python releases.
Use the native volume serial and 128-bit file ID there; retain the existing
device/inode pair on POSIX.
"""
from __future__ import annotations

import os
from pathlib import Path
import re


class FileIdentityError(OSError):
    """The platform could not provide an unambiguous stable file identity."""


_WINDOWS_ID_KEYS = {"version", "platform", "volume_serial", "file_id"}
_LEGACY_ID_KEYS = {"device", "inode"}
_HEX16 = re.compile(r"[0-9A-F]{16}\Z")
_HEX32 = re.compile(r"[0-9A-F]{32}\Z")


def _windows_file_identity(path: Path) -> dict:
    """Read FILE_ID_INFO using a metadata-only handle."""
    import ctypes

    class FileId128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class FileIdInfo(ctypes.Structure):
        _fields_ = [("VolumeSerialNumber", ctypes.c_ulonglong), ("FileId", FileId128)]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as exc:
        raise FileIdentityError("Windows file identity API is unavailable") from exc

    create_file = kernel32.CreateFileW
    create_file.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p]
    create_file.restype = ctypes.c_void_p
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                         ctypes.c_uint32]
    get_info.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    # FILE_READ_ATTRIBUTES, all sharing modes, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL.
    handle = create_file(str(path), 0x80, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        raise FileIdentityError(f"CreateFileW(FILE_READ_ATTRIBUTES) failed ({error})")
    try:
        info = FileIdInfo()
        # FILE_INFO_BY_HANDLE_CLASS.FileIdInfo == 18.
        if not get_info(handle, 18, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            raise FileIdentityError(f"GetFileInformationByHandleEx(FileIdInfo) failed ({error})")
        volume = int(info.VolumeSerialNumber)
        file_id = bytes(info.FileId.Identifier)
        if not volume or not any(file_id):
            raise FileIdentityError("Windows returned an ambiguous zero file identity")
        return {
            "version": 2,
            "platform": "windows",
            "volume_serial": f"{volume:016X}",
            "file_id": file_id.hex().upper(),
        }
    finally:
        close_handle(handle)


def file_identity(path: str | Path) -> dict:
    """Return a stable identity, independent of Windows Python stat encoding."""
    resolved = Path(path).resolve(strict=True)
    if os.name == "nt":
        return _windows_file_identity(resolved)
    stat = resolved.stat()
    if not stat.st_ino:
        raise FileIdentityError("POSIX file identity is unavailable")
    # Keep the established POSIX representation and semantics.
    return {"device": int(stat.st_dev), "inode": int(stat.st_ino)}


def valid_file_identity(identity) -> bool:
    """Recognize only supported canonical and legacy identity shapes."""
    if not isinstance(identity, dict):
        return False
    if set(identity) == _LEGACY_ID_KEYS:
        device, inode = identity["device"], identity["inode"]
        return (type(device) is int and type(inode) is int and
                0 <= device <= 0xFFFFFFFFFFFFFFFF and 0 < inode <= 0xFFFFFFFFFFFFFFFF)
    if set(identity) == _WINDOWS_ID_KEYS:
        return (type(identity["version"]) is int and identity["version"] == 2 and
                identity["platform"] == "windows" and
                isinstance(identity["volume_serial"], str) and
                _HEX16.fullmatch(identity["volume_serial"]) is not None and
                int(identity["volume_serial"], 16) != 0 and
                isinstance(identity["file_id"], str) and
                _HEX32.fullmatch(identity["file_id"]) is not None and
                int(identity["file_id"], 16) != 0)
    return False


def identity_matches(expected, current) -> bool:
    """Compare a stored identity with a freshly retrieved canonical identity.

    Full-width Windows legacy ``device`` values are checked against the native
    volume serial and ``inode`` against the low 64 bits of FILE_ID_INFO. A
    truncated device value cannot establish equivalence to a wider volume ID.
    """
    if not valid_file_identity(expected) or not valid_file_identity(current):
        return False
    if expected == current:
        # On Windows, equal legacy records are still not freshly verified:
        # only a native FILE_ID_INFO result can establish the full identity.
        if os.name == "nt" and set(current) == _LEGACY_ID_KEYS:
            return False
        return True
    if set(current) == _WINDOWS_ID_KEYS and set(expected) == _LEGACY_ID_KEYS:
        volume = int(current["volume_serial"], 16)
        file_bytes = bytes.fromhex(current["file_id"])
        file_low64 = int.from_bytes(file_bytes[:8], "little")
        file_high64 = int.from_bytes(file_bytes[8:], "little")
        # Exact full volume equality is mandatory. Do not compare only low bits.
        # Legacy Windows inode values retain only 64 bits; reject a native 128-bit
        # ID with nonzero upper bits because that old record cannot disambiguate it.
        return (expected["device"] == volume and expected["inode"] == file_low64 and
                file_high64 == 0)
    return False
