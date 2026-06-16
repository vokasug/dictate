"""
dictate.py — локальная диктовка в стиле macOS на базе faster-whisper.

Хоткей:     Insert на нумпаде (только он; обычный Insert игнорируется).
Поведение:  нажал — запись, нажал ещё раз — стоп, распознать, вставить.
Авто-стоп:  30 секунд тишины или 5 минут записи.
Трей:       при загрузке сворачивается в трей, правый клик — меню «Выход».

Зависимости: faster-whisper, sounddevice, numpy, pystray, Pillow.
Запуск:      python dictate.py
             python dictate.py --no-tray          # не скрывать консоль
             python dictate.py --device "..."     # выбрать микрофон
             python dictate.py --model small      # модель поменьше
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import glob
import os
import socket
import site
import sys
import threading
import time
from pathlib import Path


# --- лог в файл (консоли может не быть, если запущено как --noconsole .exe) ---
def _setup_log_path() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent
    return base / "dictate.log"


_LOG_PATH = _setup_log_path()
_LOG_FH = None


def _ensure_log_fh():
    global _LOG_FH
    if _LOG_FH is None:
        try:
            # Удалить старый лог, создать новый — каждый запуск с чистого листа
            if _LOG_PATH.exists():
                _LOG_PATH.unlink()
        except OSError:
            pass
        try:
            _LOG_FH = open(_LOG_PATH, "w", encoding="utf-8")
        except OSError:
            _LOG_FH = None
    return _LOG_FH


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    fh = _ensure_log_fh()
    if fh is not None:
        try:
            fh.write(line + "\n")
            fh.flush()
        except OSError:
            pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def _inject_nvidia_dll_paths() -> list[str]:
    """Сделать nvidia/*/bin/*.dll (cublas, cudnn, …) видимыми на Windows.

    Должно выполняться ДО импорта faster_whisper / ctranslate2.
    Возвращает список bin-папок, которые добавили (или [] если ничего).
    """
    candidates: list[str] = []
    try:
        candidates.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        candidates.append(site.getusersitepackages())
    except Exception:
        pass
    candidates.append(os.path.dirname(os.__file__))

    added: list[str] = []
    for base in candidates:
        for bin_dir in glob.glob(os.path.join(base, "nvidia", "*", "bin")):
            if not os.path.isdir(bin_dir):
                continue
            try:
                os.add_dll_directory(bin_dir)
            except (AttributeError, OSError):
                pass
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            added.append(bin_dir)
    if added:
        line = f"[nvidia] injected: {added}"
        try:
            print(line, file=sys.stderr, flush=True)
        except Exception:
            pass
        fh = _ensure_log_fh()
        if fh is not None:
            try:
                fh.write(line + "\n")
                fh.flush()
            except OSError:
                pass
    return added


_inject_nvidia_dll_paths()

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
import pystray  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402


SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
VOICE_RMS_THRESHOLD = 0.005
SILENCE_TIMEOUT_S = 30
MAX_RECORDING_S = 5 * 60
BE_DURATION_S = 0.08
BE_FREQS = {"start": 880.0, "stop": 660.0, "err": 220.0}
SINGLE_INSTANCE_PORT = 47891


def beep(kind: str) -> None:
    try:
        t = np.linspace(0, BE_DURATION_S, int(SAMPLE_RATE * BE_DURATION_S), False)
        tone = 0.2 * np.sin(BE_FREQS[kind] * 2 * np.pi * t)
        samples = (tone * 32767).astype(np.int16)
        sd.play(samples, SAMPLE_RATE, blocking=False)
    except Exception:
        pass


def paste_text(text: str) -> None:
    payload = text + " "
    ok = copy_to_clipboard(payload)
    if ok:
        log(f"paste: {len(text)} символов в буфер (ctypes CF_UNICODETEXT)")
    else:
        log("paste: ОШИБКА — ctypes буфер не выставился, вставка отменена")
        return

    # Только SendInput Ctrl+V — работает и в нативных Edit/RichEdit, и в Electron/Chrome/UWP.
    si_ok = send_ctrl_v()
    if si_ok:
        log("paste: SendInput Ctrl+V отправлен")
    else:
        # SendInput не сработал (не принят системой) — последний шанс через keybd_event
        time.sleep(0.02)
        send_ctrl_v_keybdevent()
        log("paste: keybd_event Ctrl+V отправлен (fallback)")


def _setup_win32_argtypes() -> None:
    """Установить argtypes/restype для Win32 clipboard функций.

    Без этого на 64-bit Windows:
    - GlobalAlloc и т.п. возвращают c_int (32-bit) → handle обрезается →
      GlobalLock получает мусор → ERROR_INVALID_HANDLE (6)
    - При передаче c_void_p (64-bit handle) параметром функции ctypes по умолчанию
      конвертирует в c_int → OverflowError: int too long to convert

    Идемпотентно — можно вызывать несколько раз.
    """
    k = ctypes.windll.kernel32
    u = ctypes.windll.user32
    k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.restype = ctypes.c_int
    k.GlobalFree.argtypes = [ctypes.c_void_p]
    k.GlobalFree.restype = ctypes.c_void_p
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u.SetClipboardData.restype = ctypes.c_void_p
    u.GetClipboardData.argtypes = [ctypes.c_uint]
    u.GetClipboardData.restype = ctypes.c_void_p
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    u.OpenClipboard.restype = ctypes.c_int
    u.EmptyClipboard.argtypes = []
    u.EmptyClipboard.restype = ctypes.c_int
    u.CloseClipboard.argtypes = []
    u.CloseClipboard.restype = ctypes.c_int


# Вызвать один раз на старте — идемпотентно, безопасно повторно.
_setup_win32_argtypes()


def _setup_hook_argtypes() -> None:
    """argtypes для функций low-level hook'а.

    Без явных argtypes ctypes по умолчанию мапит параметры в c_int (32-бит).
    LPARAM на x64 = LONG_PTR = 64-бит → c_int не влезает → OverflowError →
    событие теряется в нашем хуке и до системы не доходит.
    """
    u = ctypes.windll.user32
    u.CallNextHookEx.argtypes = [
        ctypes.c_void_p,    # hhk
        ctypes.c_int,       # nCode
        ctypes.c_ssize_t,   # wParam (WPARAM = UINT_PTR)
        ctypes.c_ssize_t,   # lParam (LPARAM = LONG_PTR)
    ]
    u.CallNextHookEx.restype = ctypes.c_ssize_t  # LRESULT = LONG_PTR
    # SetWindowsHookExW тоже принимает WPARAM-подобные DWORD, но там 32-бит
    # аргументы и проблемы нет. Оставляем дефолт.


_setup_hook_argtypes()


# --- глобальный низкоуровневый hook для нумпад-Insert ---
# pynput не различает обычный Insert и нумпад-Insert (оба → Key.insert, VK 0x2D)
# и не отдаёт флаг extended. Поэтому ставим WH_KEYBOARD_LL hook напрямую
# и фильтруем VK_INSERT + LLKHF_EXTENDED (только нумпад).
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
LLKHF_EXTENDED = 0x01
VK_INSERT = 0x2D
VK_NUMPAD0 = 0x60


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


# module-level state — должен пережить вызовы install/uninstall
_numpad_hook_proc = None  # type: ignore[var-annotated]  # чтобы GC не убил
_numpad_hook_handle = None
_numpad_hook_callback = None


def _numpad_insert_hook_proc(nCode, wParam, lParam):
    """Коллбэк WH_KEYBOARD_LL. Срабатывает на каждое нажатие клавиши в системе."""
    if nCode >= 0 and wParam == WM_KEYDOWN:
        # ctypes.cast возвращает LP_* (указатель); нужно разыменовать через [0],
        # иначе обращение к .vkCode/.flags падает с AttributeError.
        kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT))[0]
        # Нумпад-Insert: скан-код 0x52, БЕЗ extended-флага (flags & 0x01 == 0) — VK_INSERT,
        # или VK_NUMPAD0 (0x60) при включённом NumLock.
        # Обычный Insert: тот же scanCode 0x52, но flags=0x01 — игнорируем.
        if kb.scanCode == 0x52 and (
            kb.vkCode == VK_NUMPAD0
            or (kb.vkCode == VK_INSERT and not (kb.flags & LLKHF_EXTENDED))
        ):
            cb = _numpad_hook_callback
            if cb is not None:
                try:
                    cb()
                except Exception as e:
                    log(f"insert hook callback error: {e}")
    # У CallNextHookEx нет A/W-варианта (нет строк) — экспортируется как CallNextHookEx.
    # argtypes заданы на загрузке модуля (см. _setup_hook_argtypes ниже) — иначе
    # ctypes по умолчанию мапит параметры в c_int (32-бит) и lParam в 64-бита
    # не влезает → OverflowError → событие теряется.
    return ctypes.windll.user32.CallNextHookEx(None, nCode, wParam, lParam)


def _install_numpad_insert_hook(callback) -> None:
    """Поставить low-level hook, который вызывает callback ТОЛЬКО на нумпад-Insert."""
    global _numpad_hook_proc, _numpad_hook_handle, _numpad_hook_callback
    _numpad_hook_callback = callback
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    HOOKPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
    )
    # Сохраняем ссылку на proc, иначе GC убьёт и hook сломается
    _numpad_hook_proc = HOOKPROC(_numpad_insert_hook_proc)
    # WPARAM/LPARAM/LRESULT — это UINT_PTR/LONG_PTR/LONG_PTR, на x64 они 64-битные.
    # wintypes.LPARAM = c_long (32-бит) → ctypes OverflowError на каждом вызове.
    # Поэтому задаём argtypes явно.
    _numpad_hook_proc.argtypes = [
        ctypes.c_int,        # nCode
        ctypes.c_ssize_t,    # wParam (WPARAM = UINT_PTR)
        ctypes.c_ssize_t,    # lParam (LPARAM = LONG_PTR)
    ]
    _numpad_hook_proc.restype = ctypes.c_ssize_t
    # PyInstaller-frozen EXE: GetModuleHandleW(None) возвращает handle на
    # bootloader runw.exe, но наша proc-функция живёт в распакованном PKG.
    # Передаём 0 — Windows резолвит из текущего процесса.
    _numpad_hook_handle = user32.SetWindowsHookExW(
        WH_KEYBOARD_LL,
        _numpad_hook_proc,
        0,
        0,
    )
    if not _numpad_hook_handle:
        log(f"SetWindowsHookExW failed: {kernel32.GetLastError()}")


def _uninstall_numpad_insert_hook() -> None:
    """Снять hook и обнулить callback (чтобы после uninstall не дёргался Dictator)."""
    global _numpad_hook_handle, _numpad_hook_callback
    if _numpad_hook_handle:
        ctypes.windll.user32.UnhookWindowsHookEx(_numpad_hook_handle)
        _numpad_hook_handle = None
    _numpad_hook_callback = None


# --- pump сообщений, иначе WH_KEYBOARD_LL хук никогда не сработает ---
# Система доставляет события low-level hook'а потоку, который его поставил,
# но ТОЛЬКО когда этот поток качает сообщения (GetMessage/PeekMessage/
# MsgWaitForMultipleObjects). Обычный time.sleep() (kernel32 Sleep)
# НЕ качает сообщения — события накапливаются в очереди потока и
# никогда не доходят до коллбэка. Поэтому главный поток должен
# периодически вызывать _pump_messages() вместо time.sleep().
QS_ALLINPUT = 0x04FF
_PUMP_WM_QUIT = 0x0012
_PUMP_PM_REMOVE = 0x0001


class _PumpPoint(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_long),
        ("y", ctypes.c_long),
    ]


class _PumpMsg(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_size_t),
        ("time", ctypes.c_uint),
        ("pt", _PumpPoint),
        ("lPrivate", ctypes.c_uint),
    ]


def _pump_messages(timeout_ms: int) -> bool:
    """Спать до timeout_ms, параллельно прокачивая очередь сообщений потока.

    Возвращает False, если из очереди пришёл WM_QUIT (пора выходить).
    """
    rc = ctypes.windll.user32.MsgWaitForMultipleObjects(
        0, None, False, timeout_ms, QS_ALLINPUT
    )
    if rc == 0xFFFFFFFF:  # WAIT_FAILED
        return True
    # Для nCount=0 MsgWaitForMultipleObjects возвращает WAIT_OBJECT_0 + 0 == 0,
    # когда в очереди есть сообщение. (WAIT_TIMEOUT == 0x102, WAIT_FAILED == 0xFFFFFFFF.)
    if rc == 0:  # в очереди появилось сообщение
        msg = _PumpMsg()
        while ctypes.windll.user32.PeekMessageW(
            ctypes.byref(msg), None, 0, 0, _PUMP_PM_REMOVE
        ) != 0:
            if msg.message == _PUMP_WM_QUIT:
                return False
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
    return True


def copy_to_clipboard(text: str) -> bool:
    """Прямая запись UTF-16 в буфер обмена через Win32 API."""
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    try:
        if not user32.OpenClipboard(0):
            log(f"clipboard: OpenClipboard err={kernel32.GetLastError()}")
            return False
        try:
            user32.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not h:
                log(f"clipboard: GlobalAlloc err={kernel32.GetLastError()}")
                return False
            try:
                p = kernel32.GlobalLock(h)
                if not p:
                    log(f"clipboard: GlobalLock err={kernel32.GetLastError()}")
                    return False
                ctypes.memmove(p, data, len(data))
                kernel32.GlobalUnlock(h)
            except Exception:
                kernel32.GlobalFree(h)
                raise
            if not user32.SetClipboardData(CF_UNICODETEXT, h):
                log(f"clipboard: SetClipboardData err={kernel32.GetLastError()}")
                kernel32.GlobalFree(h)
                return False
            # handoff ownership to Windows
            return True
        finally:
            user32.CloseClipboard()
    except Exception as e:
        log(f"clipboard: exception: {e}")
        return False


# --- вставка текста: SendInput/keybd_event Ctrl+V ---
# ВАЖНО: _INPUT в WinAPI — это union. Без union'а sizeof выходит неверный на x64,
# и SendInput молча отбрасывает вызов с ERROR_INVALID_PARAMETER.
_PASTE_USER32 = ctypes.windll.user32
_PASTE_KERNEL32 = ctypes.windll.kernel32


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_uint),
        ("time", ctypes.c_uint),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_uint),
        ("dwFlags", ctypes.c_uint),
        ("time", ctypes.c_uint),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint),
        ("u", _INPUT_UNION),
    ]


# sanity check — без правильного union'а SendInput не работает на x64
assert ctypes.sizeof(_INPUT) == 40, f"unexpected _INPUT size {ctypes.sizeof(_INPUT)} (expected 40 on x64)"

_VK_CONTROL = 0x11
_VK_V = 0x56
_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
# маркер, чтобы отличать наш синтетический ввод (некоторые аппы фильтруют по dwExtraInfo)
_EXTRA_TAG = ctypes.c_ulong(0xD1C7A7E1)
_EXTRA_PTR = ctypes.pointer(_EXTRA_TAG)

def send_ctrl_v() -> bool:
    """Ctrl+V через SendInput. Возвращает True если ввод реально вставлен в очередь."""
    now_ms = ctypes.c_uint(int(time.time() * 1000) & 0xFFFFFFFF)
    down_inputs = (_INPUT * 2)(
        _INPUT(_INPUT_KEYBOARD, _INPUT_UNION(ki=_KEYBDINPUT(_VK_CONTROL, 0, 0, now_ms, _EXTRA_PTR))),
        _INPUT(_INPUT_KEYBOARD, _INPUT_UNION(ki=_KEYBDINPUT(_VK_V, 0, 0, now_ms, _EXTRA_PTR))),
    )
    up_inputs = (_INPUT * 2)(
        _INPUT(_INPUT_KEYBOARD, _INPUT_UNION(ki=_KEYBDINPUT(_VK_V, 0, _KEYEVENTF_KEYUP, now_ms, _EXTRA_PTR))),
        _INPUT(_INPUT_KEYBOARD, _INPUT_UNION(ki=_KEYBDINPUT(_VK_CONTROL, 0, _KEYEVENTF_KEYUP, now_ms, _EXTRA_PTR))),
    )
    cb = ctypes.sizeof(_INPUT)
    n1 = _PASTE_USER32.SendInput(2, down_inputs, cb)
    if n1 != 2:
        err = _PASTE_KERNEL32.GetLastError()
        log(f"SendInput(down) вернул {n1}/2, err={err} — Ctrl+V через SendInput НЕ вставлен")
    time.sleep(0.03)
    n2 = _PASTE_USER32.SendInput(2, up_inputs, cb)
    if n2 != 2:
        err = _PASTE_KERNEL32.GetLastError()
        log(f"SendInput(up) вернул {n2}/2, err={err} — Ctrl+V через SendInput НЕ вставлен")
    return n1 == 2 and n2 == 2


def send_ctrl_v_keybdevent() -> None:
    """Запасной путь через keybd_event — старый API, иногда проходит там, где SendInput не идёт."""
    try:
        _PASTE_USER32.keybd_event(_VK_CONTROL, 0, 0, 0)
        _PASTE_USER32.keybd_event(_VK_V, 0, 0, 0)
        time.sleep(0.02)
        _PASTE_USER32.keybd_event(_VK_V, 0, _KEYEVENTF_KEYUP, 0)
        _PASTE_USER32.keybd_event(_VK_CONTROL, 0, _KEYEVENTF_KEYUP, 0)
    except Exception as e:
        log(f"keybd_event: {e}")


class WinConsole:
    """Скрыть консольное окно через Win32 (только когда консоль есть)."""

    SW_HIDE = 0

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        try:
            self._kernel32.GetConsoleWindow.restype = ctypes.c_void_p
            self._available = bool(self._kernel32.GetConsoleWindow())
        except Exception:
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def _hwnd(self) -> int:
        return self._kernel32.GetConsoleWindow()

    def hide(self) -> None:
        if not self._available:
            return
        try:
            self._user32.ShowWindow(self._hwnd(), self.SW_HIDE)
        except Exception:
            pass


def acquire_single_instance():
    """Захватить TCP-порт 127.0.0.1:47891 как признак «один экземпляр»."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        s.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        s.listen(1)
    except OSError:
        s.close()
        return None
    return s


def make_icon_image() -> Image.Image:
    """Простая иконка-микрофон 64×64 в синем круге."""
    s = 64
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = 2
    d.ellipse((m, m, s - m, s - m), fill=(40, 50, 65, 255))
    bw = s // 4
    bx = (s - bw) // 2
    bt, bb = s // 4, int(s * 0.6)
    d.rounded_rectangle((bx, bt, bx + bw, bb), radius=bw // 2, fill=(230, 230, 240, 255))
    sx = s // 2
    sb = int(s * 0.78)
    d.line((sx, bb, sx, sb), fill=(230, 230, 240, 255), width=max(2, s // 20))
    bw2 = s // 3
    d.line((sx - bw2 // 2, sb, sx + bw2 // 2, sb),
           fill=(230, 230, 240, 255), width=max(2, s // 20))
    return img


class Dictator:
    def __init__(self, model: WhisperModel, device: int | str | None, language: str | None):
        self.model = model
        self.device = device
        self.language = language
        self.recording = False
        self.frames: list[np.ndarray] = []
        self.last_voice_t = 0.0
        self.started_t = 0.0
        self.stream: sd.InputStream | None = None
        self.watchdog: threading.Thread | None = None
        self.lock = threading.Lock()
        self.transcriber_busy = False

    def audio_cb(self, indata, frames, time_info, status):
        if status:
            log(f"audio status: {status}")
        if not self.recording:
            return
        self.frames.append(indata.copy())
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        if rms > VOICE_RMS_THRESHOLD:
            self.last_voice_t = time.time()

    def _start_stream(self) -> None:
        self.frames = []
        self.last_voice_t = time.time()
        self.started_t = time.time()
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            device=self.device,
            callback=self.audio_cb,
        )
        self.stream.start()
        self.watchdog = threading.Thread(target=self._watchdog_loop, daemon=True)
        self.watchdog.start()

    def _stop_stream(self) -> np.ndarray | None:
        self.recording = False
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        if not self.frames:
            return None
        return np.concatenate(self.frames, axis=0).reshape(-1)

    def _watchdog_loop(self) -> None:
        while self.recording:
            if time.time() - self.last_voice_t > SILENCE_TIMEOUT_S:
                log(f"авто-стоп: {SILENCE_TIMEOUT_S}с тишины")
                self.request_stop()
                return
            if time.time() - self.started_t > MAX_RECORDING_S:
                log(f"авто-стоп: {MAX_RECORDING_S // 60} мин лимит")
                self.request_stop()
                return
            time.sleep(0.5)

    def request_toggle(self) -> None:
        with self.lock:
            if self.recording:
                self._stop_and_transcribe()
            else:
                self._start()

    def _start(self) -> None:
        self.recording = True
        try:
            self._start_stream()
            log("● запись пошла  (Insert ещё раз — стоп)")
            beep("start")
        except Exception as e:
            self.recording = False
            log(f"ошибка старта: {e}")
            beep("err")

    def _stop_and_transcribe(self) -> None:
        audio = self._stop_stream()
        duration = len(audio) / SAMPLE_RATE if audio is not None else 0.0
        log(f"■ стоп ({duration:.1f}с) — распознаю…")
        beep("stop")
        if audio is None or len(audio) < SAMPLE_RATE // 4:
            log("тишина — пропускаю")
            return
        if self.transcriber_busy:
            log("предыдущая расшифровка ещё идёт — пропускаю")
            return
        threading.Thread(target=self._transcribe_and_paste, args=(audio,), daemon=True).start()

    def request_stop(self) -> None:
        with self.lock:
            if self.recording:
                self._stop_and_transcribe()

    def _transcribe_and_paste(self, audio: np.ndarray) -> None:
        self.transcriber_busy = True
        try:
            # Передаём numpy напрямую (int16 → float32, нормализация в [-1, 1])
            audio_f32 = audio.astype(np.float32) / 32768.0
            segments, info = self.model.transcribe(
                audio_f32,
                language=self.language,
                beam_size=5,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            text_parts = [seg.text.strip() for seg in segments]
            text = " ".join(p for p in text_parts if p).strip()
            if not text:
                log("ничего не распознано")
                return
            log(f"→ {text[:120]}{'…' if len(text) > 120 else ''}")
            paste_text(text)
        except Exception as e:
            log(f"ошибка распознавания: {e}")
            beep("err")
        finally:
            self.transcriber_busy = False


# Репозитории, в которых faster-whisper ищет CT2-модели. По умолчанию — Systran,
# но на диске может лежать и mobiuslabsgmbh-вариант (та же модель, другая конверсия).
_FASTER_WHISPER_REPOS = ("Systran", "mobiuslabsgmbh")


def _hf_cache_has_model(model_name: str) -> tuple[bool, str | None]:
    """Проверить, скачана ли faster-whisper модель в HF-кэш.

    Ищет по всем известным репозиториям (Systran, mobiuslabsgmbh). Возвращает
    (найдена_ли, repo_id_для_использования). Если найдена — используем тот repo,
    что уже на диске, чтобы не качать ту же модель из другого места.
    """
    cache = Path.home() / ".cache" / "huggingface" / "hub"
    for org in _FASTER_WHISPER_REPOS:
        repo = f"{org}/faster-whisper-{model_name}"
        snapshots = cache / f"models--{repo.replace('/', '--')}" / "snapshots"
        if not snapshots.is_dir():
            continue
        for snap in snapshots.iterdir():
            if (snap / "model.bin").is_file():
                return True, repo
    return False, None


def run(model_name: str, device: int | str | None, language: str | None, *, no_tray: bool) -> int:
    log(f"=== запуск ===")
    log(f"лог: {_LOG_PATH}")
    log(f"python: {sys.executable} (frozen={getattr(sys, 'frozen', False)})")

    win = WinConsole()
    log(f"консоль доступна: {win.available}, no_tray={no_tray}")

    cached, cached_repo = _hf_cache_has_model(model_name)
    load_id = cached_repo or model_name

    if no_tray:
        # без трея (явно --no-tray): грузим модель и ждём
        log(f"загружаю модель {load_id} (cuda, int8)… (кэш HF: cached={cached})")
        model = WhisperModel(load_id, device="cuda", compute_type="int8")
        log("модель готова.")
        dictator = Dictator(model, device, language)

        _install_numpad_insert_hook(dictator.request_toggle)
        try:
            while _pump_messages(200):
                pass
        except KeyboardInterrupt:
            pass
        dictator.request_stop()
        _uninstall_numpad_insert_hook()
        return 0

    # === режим с треем (по умолчанию) ===
    # 1. Сначала создаём иконку — чтобы можно было слать toast во время загрузки модели.
    log("создаю иконку в трее…")

    def on_quit(icon, item):
        log("выход по меню")
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Выход", on_quit),
    )
    log(f"модель в кэше HF: {cached} (repo={cached_repo})")
    if cached:
        notify_msg = "Загружаю модель Whisper в память..."
    else:
        notify_msg = "Модель Whisper не найдена, скачиваю..."
    icon = pystray.Icon("dictate", make_icon_image(), "Dictate: Insert (NumPad) для диктовки", menu)

    icon_thread = threading.Thread(target=icon.run, daemon=True, name="pystray")
    icon_thread.start()

    # ждём регистрации иконки в трее (до 2с, проверяем icon.visible)
    for _ in range(40):
        if icon.visible:
            break
        time.sleep(0.05)
    try:
        icon.notify(notify_msg, "Dictate")
    except Exception as e:
        log(f"notify (loading) упал: {e}")

    # 2. Грузим модель
    t0 = time.time()
    log(f"загружаю модель {load_id} (cuda, int8)…")
    model = WhisperModel(load_id, device="cuda", compute_type="int8")
    log(f"модель готова за {time.time()-t0:.1f}с")

    try:
        icon.notify(
            "Модель загружена, Insert (NumPad) - диктовка",
            "Dictate",
        )
    except Exception as e:
        log(f"notify (ready) упал: {e}")

    # 3. Запускаем dictator и ставим hook на нумпад-Insert
    dictator = Dictator(model, device, language)
    _install_numpad_insert_hook(dictator.request_toggle)

    # 4. Прячем консоль и ждём завершения
    if win.available:
        win.hide()
    log("окно скрыто в трей. Нумпад-Insert — toggle диктовка. Правый клик по иконке — выход.")

    try:
        while icon_thread.is_alive() and _pump_messages(200):
            pass
    finally:
        log("выключаюсь…")
        dictator.request_stop()
        _uninstall_numpad_insert_hook()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Локальная диктовка (faster-whisper, Insert = toggle, иконка в трее).")
    p.add_argument("--model", default="large-v3-turbo",
                   help="модель faster-whisper (default: large-v3-turbo). Меньше = быстрее, но хуже.")
    p.add_argument("--device", default=None,
                   help="имя или индекс входного аудио-устройства (default: системный по умолчанию).")
    p.add_argument("--language", default="ru", help="язык (ru, en, de, …) или 'auto' для авто-определения.")
    p.add_argument("--no-tray", action="store_true",
                   help="не сворачивать в трей (отладка). Ctrl+C в консоли — выход.")
    args = p.parse_args()

    if args.device is not None:
        try:
            args.device = int(args.device)
        except ValueError:
            pass

    language = None if args.language.lower() in ("auto", "none", "") else args.language

    lock = acquire_single_instance()
    if lock is None:
        print("dictate уже запущен — ищи иконку в трее. Если не нашёл, открой Диспетчер задач и убей python.exe.")
        return 1
    try:
        return run(args.model, args.device, language, no_tray=args.no_tray)
    finally:
        try:
            lock.close()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
