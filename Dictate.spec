# -*- mode: python ; coding: utf-8 -*-
# Dictate.spec — PyInstaller для dictate.py (Whisper-диктовка в трее).
# Оптимизации размера:
#   * strip=True — выкидывает debug-символы из .pyd. (на Windows нет strip.exe, см. ниже)
#   * excludes — модули, которые dictation точно не использует (модель уже скачана).
# UPX отключён: экономия ~26 МБ не стоит усложнения сборки и ложных срабатываний антивируса.
from PyInstaller.utils.hooks import collect_all
import os

datas = []
binaries = []
hiddenimports = []
tmp_ret = collect_all('faster_whisper')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('ctranslate2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# nvidia DLL — берём из локальной vendor/ (не зависит от venv).
# Из cublas только cublas64_12.dll (cublasLt64_12.dll — 638 MB, не нужна для int8).
# Из nvrtc исключаем .alt.dll (86 MB дубликат).
_vendor = os.path.join(SPECPATH, 'vendor')
binaries += [
    (os.path.join(_vendor, 'nvidia/cublas/bin/cublas64_12.dll'), 'nvidia/cublas/bin'),
    (os.path.join(_vendor, 'nvidia/cuda_nvrtc/bin/nvrtc64_120_0.dll'), 'nvidia/cuda_nvrtc/bin'),
    (os.path.join(_vendor, 'nvidia/cuda_nvrtc/bin/nvrtc-builtins64_129.dll'), 'nvidia/cuda_nvrtc/bin'),
]

a = Analysis(
    ['dictate.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # модули, которые dictate не использует
    excludes=[
        'test', 'unittest', 'pydoc', 'doctest', 'lib2to3',  # stdlib
        'tkinter',          # GUI не нужен
        'pynput',           # хоткей через WH_KEYBOARD_LL
        'rich', 'pygments', 'mdurl', 'markdown_it_py',  # красивый вывод HF hub
        'cffi', 'pycparser',  # FFI — не используется dictate напрямую
        'hf_xet',          # альтернативный протокол скачивания HF
    ],
    noarchive=True,       # PKG без zlib — быстрее старт, не влияет на размер (UPX отключён)
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Dictate',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,          # на Windows нет strip.exe; UPX отключён
    upx=False,             # UPX отключён — экономия ~26 МБ не стоит усложнения
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['dictate.ico'],
)
