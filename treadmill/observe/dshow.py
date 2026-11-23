"""DirectShow's video input devices by name.

OpenCV's DirectShow backend opens a device by its position in the list
DirectShow keeps of the video input category, and that list reorders when
devices re-enumerate: on the bench PC a phone's virtual camera once took the
capture dongle's index, and a run read that camera's black frames for 100
minutes. A rig can name its device instead (``[capture] device = "USB
Video"``); ``device_index()`` finds the position the name has now.

The list comes from the system device enumerator through raw COM calls, on a
thread of its own so the caller's COM apartment is left alone.
"""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import POINTER, Structure, byref, c_long, c_ubyte, c_ulong, c_ushort, c_void_p, c_wchar_p

CLSID_SYSTEM_DEVICE_ENUM = "{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}"
IID_ICREATE_DEV_ENUM = "{29840822-5B84-11D0-BD3B-00A0C911CE86}"
CLSID_VIDEO_INPUT_CATEGORY = "{860BB310-5D01-11D0-BD3B-00A0C911CE86}"
IID_IPROPERTY_BAG = "{55272A00-42CB-11CE-8135-00AA004BB851}"

COINIT_APARTMENTTHREADED = 0x2
CLSCTX_INPROC_SERVER = 0x1
S_OK = 0
VT_BSTR = 8

# vtable slots: IUnknown is 0-2 everywhere
RELEASE = 2
CREATE_CLASS_ENUMERATOR = 3        # ICreateDevEnum
ENUM_NEXT = 3                      # IEnumMoniker
BIND_TO_STORAGE = 9                # IMoniker: IUnknown 3, IPersist 1, IPersistStream 4, BindToObject, BindToStorage
BAG_READ = 3                       # IPropertyBag


class DeviceListError(OSError):
    pass


class _GUID(Structure):
    _fields_ = [("data1", c_ulong), ("data2", c_ushort), ("data3", c_ushort), ("data4", c_ubyte * 8)]


class _VARIANT(Structure):
    # vt and three reserved words, then the value union: 8 bytes on 32-bit, 16 on 64-bit
    _fields_ = [("vt", c_ushort), ("reserved", c_ushort * 3), ("value", c_void_p), ("pad", c_void_p)]


def _guid(text: str) -> _GUID:
    guid = _GUID()
    hr = ctypes.windll.ole32.CLSIDFromString(c_wchar_p(text), byref(guid))
    if hr != S_OK:
        raise DeviceListError(f"CLSIDFromString({text}) failed: 0x{hr & 0xFFFFFFFF:08X}")
    return guid


def _call(obj: c_void_p, slot: int, restype, *args_and_types):
    """Call slot ``slot`` of a COM object's vtable; ``args_and_types`` are
    (ctypes type, value) pairs after the object itself."""
    vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *(t for t, _ in args_and_types))
    return proto(vtable[slot])(obj, *(v for _, v in args_and_types))


def _release(obj: c_void_p) -> None:
    if obj:
        _call(obj, RELEASE, c_ulong)


def _friendly_name(moniker: c_void_p, iid_bag: _GUID) -> str:
    bag = c_void_p()
    hr = _call(moniker, BIND_TO_STORAGE, c_long, (c_void_p, None), (c_void_p, None),
               (POINTER(_GUID), byref(iid_bag)), (POINTER(c_void_p), byref(bag)))
    if hr < 0:
        return ""
    try:
        var = _VARIANT()
        hr = _call(bag, BAG_READ, c_long, (c_wchar_p, "FriendlyName"), (POINTER(_VARIANT), byref(var)),
                   (c_void_p, None))
        if hr < 0:
            return ""
        try:
            return ctypes.wstring_at(var.value) if var.vt == VT_BSTR and var.value else ""
        finally:
            ctypes.windll.oleaut32.VariantClear(byref(var))
    finally:
        _release(bag)


def _enumerate() -> list[str]:
    ole32 = ctypes.windll.ole32
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if hr < 0:
        raise DeviceListError(f"CoInitializeEx failed: 0x{hr & 0xFFFFFFFF:08X}")
    names: list[str] = []
    dev_enum = c_void_p()
    enum = c_void_p()
    try:
        clsid, iid = _guid(CLSID_SYSTEM_DEVICE_ENUM), _guid(IID_ICREATE_DEV_ENUM)
        category, iid_bag = _guid(CLSID_VIDEO_INPUT_CATEGORY), _guid(IID_IPROPERTY_BAG)
        hr = ole32.CoCreateInstance(byref(clsid), None, CLSCTX_INPROC_SERVER, byref(iid), byref(dev_enum))
        if hr < 0:
            raise DeviceListError(f"the system device enumerator did not start: 0x{hr & 0xFFFFFFFF:08X}")
        hr = _call(dev_enum, CREATE_CLASS_ENUMERATOR, c_long, (POINTER(_GUID), byref(category)),
                   (POINTER(c_void_p), byref(enum)), (c_ulong, 0))
        if hr < 0:
            raise DeviceListError(f"listing video input devices failed: 0x{hr & 0xFFFFFFFF:08X}")
        if not enum:                                     # S_FALSE: the category is empty
            return names
        while True:
            moniker = c_void_p()
            fetched = c_ulong()
            hr = _call(enum, ENUM_NEXT, c_long, (c_ulong, 1), (POINTER(c_void_p), byref(moniker)),
                       (POINTER(c_ulong), byref(fetched)))
            if hr != S_OK or fetched.value != 1:
                break
            try:
                names.append(_friendly_name(moniker, iid_bag))
            finally:
                _release(moniker)
        return names
    finally:
        _release(enum)
        _release(dev_enum)
        ole32.CoUninitialize()


def video_input_names() -> list[str]:
    """The friendly names of DirectShow's video input devices, in the order
    its index counts them. Windows only."""
    if os.name != "nt":
        raise DeviceListError("DirectShow devices are listed on Windows only")
    result: dict = {}

    def work() -> None:
        try:
            result["names"] = _enumerate()
        except BaseException as e:                       # handed to the caller's thread
            result["error"] = e

    thread = threading.Thread(target=work, name="treadmill-dshow-list", daemon=True)
    thread.start()
    thread.join(10.0)
    if thread.is_alive():
        raise DeviceListError("listing DirectShow devices did not finish within 10 s")
    if "error" in result:
        raise result["error"]
    return result["names"]


def device_index(name: str, names: list[str] | None = None) -> int:
    """The index DirectShow gives the device named ``name`` now. The name
    must match exactly one device."""
    names = video_input_names() if names is None else names
    hits = [i for i, n in enumerate(names) if n == name]
    if len(hits) == 1:
        return hits[0]
    listed = ", ".join(f"{i} {n!r}" for i, n in enumerate(names)) or "none"
    if not hits:
        raise DeviceListError(f"no video input device is named {name!r}; DirectShow lists {listed}")
    raise DeviceListError(f"{len(hits)} video input devices are named {name!r} ({listed}); "
                          "give the rig the index instead")
