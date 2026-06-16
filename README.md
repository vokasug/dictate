# Dictate

Local dictation app for Windows, macOS-style — press a hotkey, speak, get text pasted at cursor. Powered by [faster-whisper](https://github.com/SYSTRAN/faster-whisper).

## Features

- **NumPad Insert** to toggle recording (regular Insert ignored)
- **Auto-stop** after 30s of silence or 5 minutes total
- **System tray** — icon with right-click menu, no console window
- **CUDA GPU** acceleration (int8, ~5s model load time)
- **VAD + beam search** for punctuation and sentence boundaries

## Requirements

- Python 3.11+ 
- NVIDIA GPU with drivers
- Windows 10/11

## Quick Start

```bash
pip install -r requirements.txt
python dictate.py
```

The model (~1.5 GB) downloads automatically on first run from HuggingFace.

### Options

```
python dictate.py --no-tray          # keep console open (debug mode)
python dictate.py --model small      # smaller/faster model
python dictate.py --device "Name"    # select microphone
python dictate.py --language en      # language (default: ru)
```

## Standalone .exe

```bash
# Set up clean venv first
uv python install 3.12
uv venv --python 3.12 .venv
.venv\Scripts\pip install -r requirements.txt pyinstaller

# Copy nvidia DLLs into vendor/ (required, not in git)
robocopy .venv\Lib\site-packages\nvidia\cublas\bin vendor\nvidia\cublas\bin cublas64_12.dll
robocopy .venv\Lib\site-packages\nvidia\cuda_nvrtc\bin vendor\nvidia\cuda_nvrtc\bin nvrtc64_120_0.dll nvrtc-builtins64_129.dll

# Build
.venv\Scripts\pyinstaller Dictate.spec
# Output: dist\Dictate.exe (~630 MB)
```

## Files

| File | Purpose |
|------|---------|
| `dictate.py` | Main application |
| `Dictate.spec` | PyInstaller config |
| `requirements.txt` | Python dependencies |
| `vendor/` | NVIDIA DLLs for .exe build (not in repo) |
