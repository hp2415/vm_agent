"""访客机扬声器回环。

采的是正在播出的声音（微信语音、提示音），不是麦克风。
送出固定格式：PCM s16le、单声道、16000 Hz。打不开声卡时远控画面照常。
"""

from __future__ import annotations

import ctypes
import logging
import os
import struct
import threading
import time
import uuid
from ctypes import (
    POINTER,
    WINFUNCTYPE,
    Structure,
    byref,
    c_uint16,
    c_uint32,
    c_void_p,
    cast,
    sizeof,
    string_at,
)
from ctypes.wintypes import BYTE, DWORD, WORD

logger = logging.getLogger(__name__)

RATE = 16000
CHUNK_MS = 40
CHUNK_BYTES = RATE * 2 * CHUNK_MS // 1000

_CLSCTX_ALL = 23
_COINIT_MULTITHREADED = 0
_COINIT_APARTMENTTHREADED = 2
_AUDCLNT_SHAREMODE_SHARED = 0
_AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
_WAVE_FORMAT_PCM = 1
_WAVE_FORMAT_IEEE_FLOAT = 3
_WAVE_FORMAT_EXTENSIBLE = 0xFFFE
_AUDCLNT_BUFFERFLAGS_SILENT = 0x2
_AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
# 不绑某一块声卡。VMware 的虚拟声卡经常没有硬件回环，程序正在播的声音仍在这里。
_PROCESS_LOOPBACK_PATH = "VAD\\Process_Loopback"
_IID_ACTIVATE_HANDLER = "{41D949AB-9862-444A-80F6-C261334DA5EB}"
_IID_IUNKNOWN = "{00000000-0000-0000-C000-000000000046}"
# 标记接口，没有新方法。不实现的话激活会以「调用时机不对」失败。
_IID_IAGILE = "{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}"
_PROCESS_LOOPBACK_EXCLUDE = 1
_VT_BLOB = 65
_E_NOINTERFACE = 0x80004002

_CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_IAudioClient = "{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}"
_IID_IAudioCaptureClient = "{C8ADBD64-E71E-48a0-A4DE-185C395CD317}"


class _GUID(Structure):
    _fields_ = [
        ("Data1", DWORD),
        ("Data2", WORD),
        ("Data3", WORD),
        ("Data4", BYTE * 8),
    ]


class _WAVEFORMATEX(Structure):
    _fields_ = [
        ("wFormatTag", WORD),
        ("nChannels", WORD),
        ("nSamplesPerSec", DWORD),
        ("nAvgBytesPerSec", DWORD),
        ("nBlockAlign", WORD),
        ("wBitsPerSample", WORD),
        ("cbSize", WORD),
    ]


def _guid(text: str) -> _GUID:
    value = uuid.UUID(text)
    raw = value.bytes_le
    return _GUID(
        value.time_low,
        value.time_mid,
        value.time_hi_version,
        (BYTE * 8).from_buffer_copy(raw[8:]),
    )


def _vtbl(ptr: c_void_p) -> ctypes.Array:
    return cast(cast(ptr, POINTER(c_void_p))[0], POINTER(c_void_p))


def _call(ptr: c_void_p, index: int, restype: object, *argtypes: object):
    # ctypes 的 HRESULT 会把失败码抛成异常，这里要自己看返回值。
    if restype is ctypes.HRESULT:
        restype = ctypes.c_long
    proto = WINFUNCTYPE(restype, c_void_p, *argtypes)
    slot = _vtbl(ptr)[index]
    fn = proto(slot if isinstance(slot, int) else int(slot or 0))

    def invoke(*args: object) -> int:
        return int(fn(ptr, *args))

    return invoke


def _ole32():
    ole32 = ctypes.windll.ole32
    if getattr(_ole32, "_ready", False):
        return ole32
    ole32.CoInitializeEx.argtypes = [c_void_p, DWORD]
    ole32.CoInitializeEx.restype = ctypes.HRESULT
    ole32.CoUninitialize.argtypes = []
    ole32.CoCreateInstance.argtypes = [
        POINTER(_GUID),
        c_void_p,
        DWORD,
        POINTER(_GUID),
        POINTER(c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.HRESULT
    ole32.CoTaskMemFree.argtypes = [c_void_p]
    ole32.CoTaskMemFree.restype = None
    _ole32._ready = True  # type: ignore[attr-defined]
    return ole32


class PcmDownmix:
    """把声卡混音格式收成 16 kHz 单声道 s16le。整数倍用平均，其余线性插值。"""

    def __init__(self, channels: int, rate: int, kind: str) -> None:
        self.channels = max(1, int(channels))
        self.rate = int(rate)
        self.kind = kind
        self._mono: list[float] = []
        self._pos = 0.0
        step = self.rate / float(RATE)
        self._box = int(round(step)) if abs(step - round(step)) < 1e-3 else 0

    def feed(self, raw: bytes) -> bytes:
        self._mono.extend(_to_mono(raw, self.channels, self.kind))
        if self._box > 1:
            return self._average()
        if self._box == 1:
            return self._passthrough()
        return self._resample()

    def _average(self) -> bytes:
        width = self._box
        buf = self._mono
        out = bytearray()
        used = 0
        while used + width <= len(buf):
            acc = 0.0
            for sample in buf[used : used + width]:
                acc += sample
            out += _pack(acc / width)
            used += width
        if used:
            del buf[:used]
        return bytes(out)

    def _passthrough(self) -> bytes:
        buf = self._mono
        out = bytearray()
        for sample in buf:
            out += _pack(sample)
        buf.clear()
        return bytes(out)

    def _resample(self) -> bytes:
        step = self.rate / float(RATE)
        buf = self._mono
        pos = self._pos
        out = bytearray()
        last = len(buf) - 1
        while last >= 1 and pos + 1 <= last:
            index = int(pos)
            frac = pos - index
            sample = buf[index] * (1.0 - frac) + buf[index + 1] * frac
            out += _pack(sample)
            pos += step
        drop = int(pos)
        if drop > 0:
            del buf[:drop]
            pos -= drop
        self._pos = pos
        return bytes(out)


def _pack(sample: float) -> bytes:
    if sample > 1.0:
        sample = 1.0
    elif sample < -1.0:
        sample = -1.0
    return struct.pack("<h", int(sample * 32767.0))


def _to_mono(raw: bytes, channels: int, kind: str) -> list[float]:
    channels = max(1, channels)
    if kind == "f32":
        count = len(raw) // 4
        if count <= 0:
            return []
        values = struct.unpack(f"<{count}f", raw[: count * 4])
    elif kind == "s32":
        count = len(raw) // 4
        if count <= 0:
            return []
        values = tuple(v / 2147483648.0 for v in struct.unpack(f"<{count}i", raw[: count * 4]))
    else:
        count = len(raw) // 2
        if count <= 0:
            return []
        values = tuple(v / 32768.0 for v in struct.unpack(f"<{count}h", raw[: count * 2]))
    frames = count // channels
    if channels == 1:
        return [float(v) for v in values[:frames]]
    mono: list[float] = []
    for index in range(frames):
        base = index * channels
        acc = 0.0
        for channel in range(channels):
            acc += float(values[base + channel])
        mono.append(acc / channels)
    return mono


class _WinMsg(Structure):
    _fields_ = [
        ("hwnd", c_void_p),
        ("message", c_uint32),
        ("wParam", c_void_p),
        ("lParam", c_void_p),
        ("time", DWORD),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


class _ActivateParams(Structure):
    _fields_ = [
        ("ActivationType", DWORD),
        ("TargetProcessId", DWORD),
        ("ProcessLoopbackMode", DWORD),
    ]


class _PropVariant(Structure):
    """x64 上 VT_BLOB 的布局：8 字节头 + cbSize + 对齐 + 指针。"""

    _fields_ = [
        ("vt", WORD),
        ("wReserved1", WORD),
        ("wReserved2", WORD),
        ("wReserved3", WORD),
        ("cbSize", DWORD),
        ("_pad", DWORD),
        ("pBlobData", c_void_p),
    ]


def _guid_text(value: _GUID) -> str:
    tail = bytes(bytearray(value.Data4))
    return (
        f"{int(value.Data1):08X}-{int(value.Data2):04X}-{int(value.Data3):04X}-"
        f"{tail[:2].hex()}-{tail[2:].hex()}"
    ).upper()


def pcm_peak(pcm: bytes) -> int:
    count = len(pcm) // 2
    if count <= 0:
        return 0
    samples = struct.unpack(f"<{count}h", pcm[: count * 2])
    return max(abs(sample) for sample in samples)


_IMA_INDEX_TABLE = (
    -1, -1, -1, -1, 2, 4, 6, 8,
    -1, -1, -1, -1, 2, 4, 6, 8,
)
_IMA_STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
)


def encode_ima_adpcm(pcm: bytes, step_index: int = 0) -> tuple[bytes, int]:
    """把 16-bit s16le 单声道 PCM 压成帧独立的 4-bit IMA ADPCM（约 4:1）。

    块头 4 字节 <hBB：首采样 predictor、初始 step_index、flags（bit0=1 表示末字节高 4 位填充）。
    每个块独立携带预测器，丢包不影响后续块解码。
    """
    count = len(pcm) // 2
    if count <= 0:
        return b"", 0
    samples = struct.unpack(f"<{count}h", pcm[: count * 2])
    pred = samples[0]
    idx = 0 if step_index < 0 else (88 if step_index > 88 else int(step_index))
    tail_count = count - 1
    flags = tail_count & 1
    out = bytearray(4 + ((tail_count + 1) >> 1))
    struct.pack_into("<hBB", out, 0, pred, idx, flags)
    index_table = _IMA_INDEX_TABLE
    step_table = _IMA_STEP_TABLE
    pos = 4
    low = -1
    for sample in samples[1:]:
        diff = sample - pred
        if diff < 0:
            sign = 8
            diff = -diff
        else:
            sign = 0
        step = step_table[idx]
        delta = 0
        vpdiff = step >> 3
        if diff >= step:
            delta = 4
            diff -= step
            vpdiff += step
        step >>= 1
        if diff >= step:
            delta |= 2
            diff -= step
            vpdiff += step
        step >>= 1
        if diff >= step:
            delta |= 1
            vpdiff += step
        if sign:
            pred -= vpdiff
            if pred < -32768:
                pred = -32768
        else:
            pred += vpdiff
            if pred > 32767:
                pred = 32767
        code = delta | sign
        idx += index_table[code]
        if idx < 0:
            idx = 0
        elif idx > 88:
            idx = 88
        if low < 0:
            low = code
        else:
            out[pos] = low | (code << 4)
            pos += 1
            low = -1
    if low >= 0:
        out[pos] = low
    return bytes(out), idx


class LoopbackCapture:
    """默认播放设备的 WASAPI 回环。只在采集线程里打开和关闭。"""

    def __init__(self) -> None:
        self._com = False
        self._enumerator: c_void_p | None = None
        self._device: c_void_p | None = None
        self._client: c_void_p | None = None
        self._capture: c_void_p | None = None
        self._mix: c_void_p | None = None
        self._downmix: PcmDownmix | None = None
        self._block = 0
        self._pcm = bytearray()
        self._source = ""
        self._ready = threading.Event()
        self._activate_hr = 1
        self._activated: c_void_p | None = None
        self._handler = None
        self._handler_vtbl = None
        self._handler_fns: list[object] = []
        self._activate_params: _ActivateParams | None = None
        self._prop: _PropVariant | None = None

    def open(self) -> bool:
        try:
            return self._open()
        except Exception as exc:  # noqa: BLE001
            logger.warning("扬声器回环初始化失败: %s", exc)
            self.close()
            return False

    def read(self, timeout: float) -> bytes:
        """凑满约 40ms 就返回一块。这段时间没有足够采样时返回空。"""
        if self._capture is None or self._downmix is None:
            return b""
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            self._pump()
            self._pull()
            if len(self._pcm) >= CHUNK_BYTES:
                chunk = bytes(self._pcm[:CHUNK_BYTES])
                del self._pcm[:CHUNK_BYTES]
                return chunk
            time.sleep(0.005)
        return b""

    def close(self) -> None:
        client = self._client
        capture = self._capture
        self._client = None
        self._capture = None
        if client is not None:
            try:
                _call(client, 11, ctypes.HRESULT)()  # Stop
            except Exception:  # noqa: BLE001
                pass
        self._release(capture)
        self._release(client)
        self._release(self._device)
        self._device = None
        self._release(self._enumerator)
        self._enumerator = None
        if self._mix is not None:
            _ole32().CoTaskMemFree(self._mix)
            self._mix = None
        self._downmix = None
        self._pcm.clear()
        self._handler = None
        self._handler_vtbl = None
        self._handler_fns.clear()
        self._activate_params = None
        self._prop = None
        if self._com:
            _ole32().CoUninitialize()
            self._com = False

    def describe(self) -> str:
        return self._source or "未打开"

    def _open(self) -> bool:
        ole32 = _ole32()
        # 进程回环必须在 STA 里激活。MTA 会直接返回「调用时机不对」。
        hr = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        if hr not in (0, 1):
            logger.warning("COM 初始化失败: 0x%08X", hr & 0xFFFFFFFF)
            return False
        self._com = True
        try:
            combase = ctypes.windll.combase
            combase.RoInitialize.argtypes = [ctypes.c_int]
            combase.RoInitialize.restype = ctypes.c_long
            combase.RoInitialize(0)
        except Exception:  # noqa: BLE001
            pass
        if self._open_process_loopback():
            return True
        logger.info("进程回环不可用，改采播放设备")
        return self._open_hardware()

    def _open_process_loopback(self) -> bool:
        """抓除本进程以外正在播放的声音，不依赖 VMware 虚拟声卡有没有回环脚。"""
        try:
            mmdev = ctypes.windll.mmdevapi
            mmdev.ActivateAudioInterfaceAsync.restype = ctypes.c_long
            mmdev.ActivateAudioInterfaceAsync.argtypes = [
                ctypes.c_wchar_p,
                POINTER(_GUID),
                POINTER(_PropVariant),
                c_void_p,
                POINTER(c_void_p),
            ]
        except AttributeError:
            logger.info("这个系统没有进程回环")
            return False
        params = _ActivateParams(1, os.getpid(), _PROCESS_LOOPBACK_EXCLUDE)
        prop = _PropVariant()
        prop.vt = _VT_BLOB
        prop.cbSize = sizeof(params)
        prop.pBlobData = ctypes.addressof(params)
        self._activate_params = params
        self._prop = prop
        self._ready.clear()
        self._activate_hr = 1
        self._activated = None
        handler = self._completion_handler()
        iid_client = _guid(_IID_IAudioClient)
        operation = c_void_p()
        hr = mmdev.ActivateAudioInterfaceAsync(
            _PROCESS_LOOPBACK_PATH,
            byref(iid_client),
            byref(prop),
            handler,
            byref(operation),
        )
        if hr != 0:
            logger.info("进程回环激活失败: 0x%08X", hr & 0xFFFFFFFF)
            return False
        if not self._wait_sta(3.0):
            logger.info("进程回环没有在 3 秒内完成")
            self._release(operation)
            return False
        self._release(operation)
        if self._activate_hr != 0 or not self._activated:
            logger.info("进程回环结果: 0x%08X", self._activate_hr & 0xFFFFFFFF)
            return False
        client = self._activated
        self._activated = None
        self._client = client
        fmt = _WAVEFORMATEX()
        fmt.wFormatTag = 1
        fmt.nChannels = 2
        fmt.nSamplesPerSec = 48000
        fmt.nBlockAlign = 4
        fmt.nAvgBytesPerSec = 48000 * 4
        fmt.wBitsPerSample = 16
        fmt.cbSize = 0
        self._pcm_format = fmt
        flags = _AUDCLNT_STREAMFLAGS_LOOPBACK | _AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
        hr = _call(
            client,
            3,
            ctypes.HRESULT,
            DWORD,
            DWORD,
            ctypes.c_longlong,
            ctypes.c_longlong,
            POINTER(_WAVEFORMATEX),
            c_void_p,
        )(_AUDCLNT_SHAREMODE_SHARED, flags, 0, 0, byref(fmt), None)
        if hr != 0:
            logger.info("进程回环初始化失败: 0x%08X", hr & 0xFFFFFFFF)
            self._release(client)
            self._client = None
            return False
        if not self._start_capture(client, 2, 48000, 4, "s16"):
            self._release(self._capture)
            self._capture = None
            self._release(client)
            self._client = None
            return False
        self._source = "进程回环"
        logger.info("扬声器采集已开：进程回环 48000Hz stereo s16")
        return True

    def _completion_handler(self) -> c_void_p:
        query_t = WINFUNCTYPE(ctypes.HRESULT, c_void_p, POINTER(_GUID), POINTER(c_void_p))
        count_t = WINFUNCTYPE(ctypes.c_ulong, c_void_p)
        done_t = WINFUNCTYPE(ctypes.HRESULT, c_void_p, c_void_p)

        def query(this: int, riid: object, ppv: object) -> int:
            name = _guid_text(riid.contents)  # type: ignore[attr-defined]
            known = {
                item.strip("{}").upper()
                for item in (_IID_IUNKNOWN, _IID_ACTIVATE_HANDLER, _IID_IAGILE)
            }
            if name not in known:
                logger.info("回环回调不认识 %s", name)
                ppv[0] = None  # type: ignore[index]
                return _E_NOINTERFACE
            ppv[0] = this  # type: ignore[index]
            return 0

        def add_ref(_this: int) -> int:
            return 2

        def release(_this: int) -> int:
            return 1

        def completed(_this: int, operation: int) -> int:
            activate_hr = ctypes.HRESULT()
            punk = c_void_p()
            call_hr = _call(
                c_void_p(operation),
                3,
                ctypes.HRESULT,
                POINTER(ctypes.HRESULT),
                POINTER(c_void_p),
            )(byref(activate_hr), byref(punk))
            self._activate_hr = int(activate_hr.value if call_hr == 0 else call_hr)
            self._activated = punk if punk else None
            self._ready.set()
            return 0

        query_fn = query_t(query)
        add_fn = count_t(add_ref)
        release_fn = count_t(release)
        done_fn = done_t(completed)
        self._handler_fns = [query_fn, add_fn, release_fn, done_fn]

        class _Vtbl(Structure):
            _fields_ = [
                ("QueryInterface", c_void_p),
                ("AddRef", c_void_p),
                ("Release", c_void_p),
                ("ActivateCompleted", c_void_p),
            ]

        class _Handler(Structure):
            _fields_ = [("lpVtbl", POINTER(_Vtbl))]

        vtbl = _Vtbl(
            ctypes.cast(query_fn, c_void_p),
            ctypes.cast(add_fn, c_void_p),
            ctypes.cast(release_fn, c_void_p),
            ctypes.cast(done_fn, c_void_p),
        )
        handler = _Handler(ctypes.pointer(vtbl))
        self._handler_vtbl = vtbl
        self._handler = handler
        return ctypes.cast(ctypes.pointer(handler), c_void_p)

    def _start_capture(
        self,
        client: c_void_p,
        channels: int,
        rate: int,
        block: int,
        kind: str,
    ) -> bool:
        capture = c_void_p()
        iid_capture = _guid(_IID_IAudioCaptureClient)
        hr = _call(client, 14, ctypes.HRESULT, POINTER(_GUID), POINTER(c_void_p))(
            byref(iid_capture), byref(capture)
        )
        if hr != 0 or not capture:
            logger.warning("拿不到采集客户端: 0x%08X", hr & 0xFFFFFFFF)
            return False
        self._capture = capture
        hr = _call(client, 10, ctypes.HRESULT)()
        if hr != 0:
            logger.warning("回环启动失败: 0x%08X", hr & 0xFFFFFFFF)
            return False
        self._downmix = PcmDownmix(channels, rate, kind)
        self._block = block
        return True

    def _pump(self) -> None:
        """采集线程是 STA。不抽消息的话，回环激活之后可能只吐出第一块就停。"""
        user32 = ctypes.windll.user32
        user32.PeekMessageW.argtypes = [
            POINTER(_WinMsg),
            c_void_p,
            c_uint32,
            c_uint32,
            c_uint32,
        ]
        user32.PeekMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [POINTER(_WinMsg)]
        user32.TranslateMessage.restype = ctypes.c_int
        user32.DispatchMessageW.argtypes = [POINTER(_WinMsg)]
        user32.DispatchMessageW.restype = ctypes.c_ssize_t
        msg = _WinMsg()
        while user32.PeekMessageW(byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(byref(msg))
            user32.DispatchMessageW(byref(msg))

    def _wait_sta(self, timeout: float) -> bool:
        """STA 上的完成回调要靠本线程抽消息才会进来。"""
        user32 = ctypes.windll.user32
        user32.PeekMessageW.argtypes = [
            POINTER(_WinMsg),
            c_void_p,
            c_uint32,
            c_uint32,
            c_uint32,
        ]
        user32.PeekMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [POINTER(_WinMsg)]
        user32.TranslateMessage.restype = ctypes.c_int
        user32.DispatchMessageW.argtypes = [POINTER(_WinMsg)]
        user32.DispatchMessageW.restype = ctypes.c_ssize_t
        msg = _WinMsg()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._ready.is_set():
                return True
            while user32.PeekMessageW(byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(byref(msg))
                user32.DispatchMessageW(byref(msg))
            self._ready.wait(0.01)
        return self._ready.is_set()

    def _open_hardware(self) -> bool:
        ole32 = _ole32()
        enumerator = c_void_p()
        clsid = _guid(_CLSID_MMDeviceEnumerator)
        iid_enum = _guid(_IID_IMMDeviceEnumerator)
        hr = ole32.CoCreateInstance(
            byref(clsid),
            None,
            _CLSCTX_ALL,
            byref(iid_enum),
            byref(enumerator),
        )
        if hr != 0 or not enumerator:
            logger.warning("音频设备枚举失败: 0x%08X", hr & 0xFFFFFFFF)
            return False
        self._enumerator = enumerator
        device = c_void_p()
        # GetDefaultAudioEndpoint(eRender=0, eConsole=0)
        hr = _call(enumerator, 4, ctypes.HRESULT, c_uint32, c_uint32, POINTER(c_void_p))(
            0, 0, byref(device)
        )
        if hr != 0 or not device:
            logger.warning("没有默认播放设备: 0x%08X", hr & 0xFFFFFFFF)
            return False
        self._device = device
        client = c_void_p()
        iid_client = _guid(_IID_IAudioClient)
        hr = _call(
            device, 3, ctypes.HRESULT, POINTER(_GUID), DWORD, c_void_p, POINTER(c_void_p)
        )(byref(iid_client), _CLSCTX_ALL, None, byref(client))
        if hr != 0 or not client:
            logger.warning("打开音频客户端失败: 0x%08X", hr & 0xFFFFFFFF)
            return False
        self._client = client
        mix = c_void_p()
        hr = _call(client, 8, ctypes.HRESULT, POINTER(c_void_p))(byref(mix))
        if hr != 0 or not mix:
            logger.warning("读不到混音格式: 0x%08X", hr & 0xFFFFFFFF)
            return False
        self._mix = mix
        kind = _format_kind(mix)
        if kind is None:
            logger.warning("不支持的混音格式")
            return False
        channels, rate, block, sample_kind = kind
        # Initialize(shared, loopback, 默认缓冲, 0, mix, NULL)
        hr = _call(
            client,
            3,
            ctypes.HRESULT,
            DWORD,
            DWORD,
            ctypes.c_longlong,
            ctypes.c_longlong,
            c_void_p,
            c_void_p,
        )(_AUDCLNT_SHAREMODE_SHARED, _AUDCLNT_STREAMFLAGS_LOOPBACK, 0, 0, mix, None)
        if hr != 0:
            logger.warning("回环初始化失败: 0x%08X", hr & 0xFFFFFFFF)
            return False
        capture = c_void_p()
        iid_capture = _guid(_IID_IAudioCaptureClient)
        hr = _call(client, 14, ctypes.HRESULT, POINTER(_GUID), POINTER(c_void_p))(
            byref(iid_capture), byref(capture)
        )
        if hr != 0 or not capture:
            logger.warning("拿不到采集客户端: 0x%08X", hr & 0xFFFFFFFF)
            return False
        self._capture = capture
        hr = _call(client, 10, ctypes.HRESULT)()  # Start
        if hr != 0:
            logger.warning("回环启动失败: 0x%08X", hr & 0xFFFFFFFF)
            return False
        self._downmix = PcmDownmix(channels, rate, sample_kind)
        self._block = block
        self._source = "播放设备回环"
        logger.info(
            "扬声器采集已开：播放设备回环 channels=%s rate=%s kind=%s",
            channels,
            rate,
            sample_kind,
        )
        return True

    def _pull(self) -> None:
        capture = self._capture
        downmix = self._downmix
        if capture is None or downmix is None or self._block <= 0:
            return
        while True:
            packets = c_uint32()
            hr = _call(capture, 5, ctypes.HRESULT, POINTER(c_uint32))(byref(packets))
            if hr != 0 or packets.value <= 0:
                return
            data = c_void_p()
            frames = c_uint32()
            flags = DWORD()
            hr = _call(
                capture,
                3,
                ctypes.HRESULT,
                POINTER(c_void_p),
                POINTER(c_uint32),
                POINTER(DWORD),
                POINTER(ctypes.c_uint64),
                POINTER(ctypes.c_uint64),
            )(byref(data), byref(frames), byref(flags), None, None)
            if hr != 0 or frames.value <= 0:
                return
            nbytes = int(frames.value) * self._block
            # 有的虚拟声卡会把有声音的缓冲也标成静音。指针还在就照实读取。
            if not data:
                raw = b"\x00" * nbytes
            else:
                raw = string_at(data, nbytes)
            del flags
            _call(capture, 4, ctypes.HRESULT, c_uint32)(frames.value)
            self._pcm.extend(downmix.feed(raw))
            if len(self._pcm) > RATE * 2:
                extra = len(self._pcm) - RATE * 2
                extra -= extra % 2
                if extra > 0:
                    del self._pcm[:extra]

    @staticmethod
    def _release(ptr: c_void_p | None) -> None:
        if ptr:
            _call(ptr, 2, ctypes.c_ulong)()


def _format_kind(mix: c_void_p) -> tuple[int, int, int, str] | None:
    fmt = cast(mix, POINTER(_WAVEFORMATEX)).contents
    channels = int(fmt.nChannels)
    rate = int(fmt.nSamplesPerSec)
    block = int(fmt.nBlockAlign)
    bits = int(fmt.wBitsPerSample)
    tag = int(fmt.wFormatTag)
    if channels <= 0 or channels > 8 or rate < 8000 or rate > 192000 or block <= 0:
        return None
    if tag == _WAVE_FORMAT_EXTENSIBLE and int(fmt.cbSize) >= 22:
        # WAVEFORMATEX(18) + wValidBits(2) + channelMask(4) 之后是 SubFormat。
        blob = string_at(mix, 18 + int(fmt.cbSize))
        tag = struct.unpack_from("<I", blob, 24)[0]
    if tag == _WAVE_FORMAT_IEEE_FLOAT and bits == 32:
        return channels, rate, block, "f32"
    if tag == _WAVE_FORMAT_PCM and bits == 16:
        return channels, rate, block, "s16"
    if tag == _WAVE_FORMAT_PCM and bits == 32:
        return channels, rate, block, "s32"
    logger.warning("混音格式 tag=%s bits=%s", tag, bits)
    return None
