# -*- coding: utf-8 -*-
"""当前输出设备：音量（0-100）与切换默认设备。

用的是 Windows Core Audio（WASAPI）的 COM 接口，**纯 ctypes**，不引第三方包：
pycaw / comtypes 装起来容易、打包进 exe 又要多几百 KB，而且版本一变接口就飘。
这里要的就三件事：

- 列出现有的输出设备（名字 + 设备 id + 哪个是默认）；
- 读/写**当前默认输出设备**的主音量（0-100）和静音；
- 把默认输出设备切成另一个（IPolicyConfig，微软没公开文档但一直能用）。

为什么不直接调 IAudioEndpointVolume 就完事：那是"当前默认设备"的音量，
换设备之后要重新拿一次 —— 所以每次操作都现查默认设备，不缓存接口。

拿不到 COM 时所有函数都返回空/None，工具层据此说人话，绝不让助手崩掉。
"""

from __future__ import annotations

import ctypes
from ctypes import (
    POINTER,
    byref,
    c_byte,
    c_float,
    c_int,
    c_ulong,
    c_ushort,
    c_void_p,
    c_wchar_p,
)

__all__ = [
    "available", "list_output_devices", "default_device",
    "get_volume", "set_volume", "get_mute", "set_mute", "set_default",
]

_ole32 = ctypes.windll.ole32 if hasattr(ctypes, "windll") else None  # 非 Windows 下全不可用

# ── COM 基础件 ─────────────────────────────────────────────────────────────


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort),
                ("Data4", c_byte * 8)]


class _PROPVARIANT(ctypes.Structure):
    # 64 位下 PROPVARIANT 是 24 字节：vt + 3 个保留字 + 16 字节联合体
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort),
                ("r3", c_ushort), ("data", c_byte * 16)]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", c_ulong)]


def _guid(text: str) -> _GUID:
    guid = _GUID()
    if _ole32.CLSIDFromString(c_wchar_p(text), byref(guid)) != 0:
        raise OSError("GUID 写错了：" + text)
    return guid


def _call(ptr, index: int, *argtypes):
    """按虚表下标调一个 COM 方法，返回值固定按 HRESULT（long）解。

    *argtypes 只写**参数**类型：COM 的前三个槽位是 QueryInterface/AddRef/Release，
    这些方法返回 HRESULT 或 ULONG，用 long 接都安全。
    """
    vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, *argtypes)
    return proto(vtable[index])


def _release(ptr) -> None:
    try:
        _call(ptr, 2)(ptr)
    except Exception:  # noqa: BLE001 - 释放失败也只能算了
        pass


_CLSID_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_ENDPOINT_VOLUME = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
_PKEY_FRIENDLY_NAME = _PROPERTYKEY(_guid("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)

#: 切换默认设备的两个"非公开"接口。Win10/11 上通常只有 Vista 那个能创建成功
#: （本机实测：CPolicyConfigClient 报 0x80040154 未注册，Vista 那个可以）。
#: 两者的虚表**差一个方法**，所以 SetDefaultEndpoint 的下标不一样，必须配对使用 ——
#: 用错下标就是去调别的函数，轻则失败重则把设备列表改坏。
_POLICY_VARIANTS = (
    ("{870AF99C-DAF4-4D9F-AF0E-F40F1E4E37C8}", "{F8679F50-850A-41CF-9C72-430F290290C8}", 13),
    ("{294935CE-F637-4E7C-A41B-AB255460B862}", "{568B9108-44BF-40B4-9006-86AFE5B5A620}", 12),
)

_eRender = 0
_eConsole, _eMultimedia, _eCommunications = 0, 1, 2
_DEVICE_STATE_ACTIVE = 0x1
_CLSCTX_ALL = 0x17
_VT_LPWSTR = 31


class _Com:
    """一次操作的 COM 现场：CoInitialize + 枚举器，退出时统一释放。"""

    def __init__(self) -> None:
        self.enumerator = c_void_p()
        self._com_ready = False

    def __enter__(self):
        if _ole32 is None:
            raise OSError("这个系统没有 Core Audio（不是 Windows？）")
        _ole32.CoInitialize(None)
        self._com_ready = True
        hr = _ole32.CoCreateInstance(byref(_guid(_CLSID_ENUMERATOR)), None, _CLSCTX_ALL,
                                     byref(_guid(_IID_ENUMERATOR)),
                                     byref(self.enumerator))
        if hr != 0 or not self.enumerator:
            raise OSError("拿不到音频设备枚举器（hr=0x%08X）" % (hr & 0xFFFFFFFF))
        return self

    def __exit__(self, *_exc) -> None:
        if self.enumerator:
            _release(self.enumerator)
            self.enumerator = None
        if self._com_ready:
            try:
                _ole32.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass

    # -- 设备 ---------------------------------------------------------
    def default_id(self) -> str:
        device = c_void_p()
        hr = _call(self.enumerator, 4, c_int, c_int, POINTER(c_void_p))(
            self.enumerator, _eRender, _eConsole, byref(device))
        if hr != 0 or not device:
            return ""
        try:
            return self._device_id(device)
        finally:
            _release(device)

    def _device_id(self, device) -> str:
        buffer = c_wchar_p()
        _call(device, 5, POINTER(c_wchar_p))(device, byref(buffer))
        text = buffer.value or ""
        if buffer:
            _ole32.CoTaskMemFree(buffer)  # 设备 id 是 COM 分配的内存，得自己还
        return text

    def _friendly_name(self, device) -> str:
        store = c_void_p()
        if _call(device, 4, c_ulong, POINTER(c_void_p))(device, 0, byref(store)) != 0:
            return ""
        try:
            value = _PROPVARIANT()
            if _call(store, 5, POINTER(_PROPERTYKEY),
                     POINTER(_PROPVARIANT))(store, byref(_PKEY_FRIENDLY_NAME),
                                            byref(value)) != 0:
                return ""
            try:
                if value.vt != _VT_LPWSTR:
                    return ""
                return ctypes.cast(value.data, POINTER(c_wchar_p)).contents.value or ""
            finally:
                _ole32.PropVariantClear(byref(value))
        finally:
            _release(store)

    def devices(self) -> list:
        collection = c_void_p()
        if _call(self.enumerator, 3, c_int, ctypes.c_uint,
                 POINTER(c_void_p))(self.enumerator, _eRender, _DEVICE_STATE_ACTIVE,
                                    byref(collection)) != 0:
            return []
        default_id = self.default_id()
        items = []
        try:
            count = ctypes.c_uint()
            _call(collection, 3, POINTER(ctypes.c_uint))(collection, byref(count))
            for index in range(count.value):
                device = c_void_p()
                if _call(collection, 4, ctypes.c_uint,
                         POINTER(c_void_p))(collection, index, byref(device)) != 0:
                    continue
                try:
                    device_id = self._device_id(device)
                    items.append({"index": index, "name": self._friendly_name(device),
                                  "id": device_id, "default": device_id == default_id})
                finally:
                    _release(device)
        finally:
            _release(collection)
        return items

    # -- 音量 ---------------------------------------------------------
    def endpoint_volume(self):
        device = c_void_p()
        if _call(self.enumerator, 4, c_int, c_int, POINTER(c_void_p))(
                self.enumerator, _eRender, _eConsole, byref(device)) != 0 or not device:
            return None
        volume = c_void_p()
        hr = _call(device, 3, POINTER(_GUID), c_ulong, c_void_p,
                   POINTER(c_void_p))(device, byref(_guid(_IID_ENDPOINT_VOLUME)),
                                      _CLSCTX_ALL, None, byref(volume))
        _release(device)          # endpoint volume 自己持有引用，设备接口可以放了
        if hr != 0 or not volume:
            return None
        return volume


def available() -> str:
    """能用就返回空串，不能就返回原因（工具层照原样念给用户）。"""
    if _ole32 is None:
        return "这个系统没有 Core Audio（不是 Windows）"
    try:
        with _Com() as com:
            return "" if com.default_id() else "找不到可用的输出设备"
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:80]


def list_output_devices() -> list:
    """所有输出设备（含哪个是默认）。失败返回空列表。"""
    try:
        with _Com() as com:
            return com.devices()
    except Exception:  # noqa: BLE001
        return []


def default_device():
    """当前默认输出设备（dict）或 None。"""
    for item in list_output_devices():
        if item["default"]:
            return item
    return None


def get_volume():
    """当前默认输出设备的音量（0-100）；读不到返回 None。"""
    try:
        with _Com() as com:
            volume = com.endpoint_volume()
            if volume is None:
                return None
            try:
                level = c_float()
                if _call(volume, 9, POINTER(c_float))(volume, byref(level)) != 0:
                    return None
                return int(round(level.value * 100))
            finally:
                _release(volume)
    except Exception:  # noqa: BLE001
        return None


def set_volume(percent) -> int | None:
    """把当前默认输出设备的音量设成 0-100（返回实际生效的百分比）。"""
    target = max(0.0, min(float(percent), 100.0)) / 100.0
    try:
        with _Com() as com:
            volume = com.endpoint_volume()
            if volume is None:
                return None
            try:
                if _call(volume, 7, c_float, c_void_p)(volume, c_float(target), None) != 0:
                    return None
                level = c_float()
                _call(volume, 9, POINTER(c_float))(volume, byref(level))
                return int(round(level.value * 100))
            finally:
                _release(volume)
    except Exception:  # noqa: BLE001
        return None


def get_mute():
    try:
        with _Com() as com:
            volume = com.endpoint_volume()
            if volume is None:
                return None
            try:
                muted = c_int()
                if _call(volume, 15, POINTER(c_int))(volume, byref(muted)) != 0:
                    return None
                return bool(muted.value)
            finally:
                _release(volume)
    except Exception:  # noqa: BLE001
        return None


def set_mute(muted: bool):
    try:
        with _Com() as com:
            volume = com.endpoint_volume()
            if volume is None:
                return None
            try:
                if _call(volume, 14, c_int, c_void_p)(volume, c_int(1 if muted else 0), None) != 0:
                    return None
                return bool(muted)
            finally:
                _release(volume)
    except Exception:  # noqa: BLE001
        return None


def set_default(device) -> tuple:
    """把默认输出设备切成 device（list_output_devices 里的一项）。

    Windows 把"默认设备"分成三个角色（控制台 / 多媒体 / 通信），三个都切，
    否则会出现"媒体还从旧设备出声"这种半拉子状态。
    """
    device_id = str((device or {}).get("id") or "")
    if not device_id:
        return False, "这个设备没有 id，换不了"
    if _ole32 is None:
        return False, "这个系统不支持切换默认输出设备"
    last = ""
    for clsid, iid, index in _POLICY_VARIANTS:
        ptr = c_void_p()
        try:
            _ole32.CoInitialize(None)
            hr = _ole32.CoCreateInstance(byref(_guid(clsid)), None, _CLSCTX_ALL,
                                        byref(_guid(iid)), byref(ptr))
            if hr != 0 or not ptr:
                last = "hr=0x%08X" % (hr & 0xFFFFFFFF)
                continue
            try:
                for role in (_eConsole, _eMultimedia, _eCommunications):
                    if _call(ptr, index, c_wchar_p, c_int)(ptr, device_id, role) != 0:
                        last = "SetDefaultEndpoint 失败"
                        break
                else:
                    return True, ""
            finally:
                _release(ptr)
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:60]
        finally:
            try:
                _ole32.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass
    return False, ("切换默认输出设备失败（" + (last or "系统没提供这个接口") + "）")
