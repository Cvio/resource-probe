# Running resource_probe from a USB drive

The script needs no admin rights and no drivers. Everything it reads (CPU, RAM,
GPU) is normal-user readable, and it works fine from removable media. Reports are
written next to the script, so results travel with the drive.

The only obstacle is that the target machine needs **Python and psutil**. Three
ways to solve that, worst to best.

## Option 1 — install psutil on each machine

```
pip install psutil
```

Fine if every machine already has Python. Nothing else to do.

## Option 2 — vendor psutil onto the USB

On a machine with Python and matching architecture:

```
pip install --target=E:\probe\libs psutil
```

Then point Python at that folder:

```
set PYTHONPATH=E:\probe\libs
python E:\probe\resource_probe.py record --label myapp
```

Still needs Python on the host, but nothing gets installed to it.

## Option 3 — single .exe, no Python needed (recommended)

Build once on a Windows machine, then the USB is genuinely plug-and-run:

```
pip install pyinstaller psutil
pyinstaller --onefile --name resource_probe resource_probe.py
```

That produces `dist\resource_probe.exe`, roughly 10 MB. Copy it to the USB. On any
Windows machine:

```
E:\resource_probe.exe record --label sokuji --duration 120
E:\resource_probe.exe record --label comfyui --baseline sokuji.json --html
```

No Python, no pip, no admin.

The executable is OS- and architecture-specific. Build a separate one on macOS or
Linux if you need those. Note also that some antivirus software flags PyInstaller
binaries as suspicious — a known false-positive pattern, but worth knowing before
you hand the drive to someone else.

## Forcing reports onto the drive

By default reports land in the current working directory, which may not be the USB
depending on how you launched. To be explicit:

```
E:\resource_probe.exe record --label sokuji ^
  --out E:\reports\sokuji.json ^
  --html E:\reports\sokuji.html
```

## GPU stats

These come from `nvidia-smi`, which ships with the NVIDIA driver and is already
present on any machine with an NVIDIA GPU. Nothing to carry. On machines without
one, the script skips GPU metrics silently and still reports CPU and RAM.

## A note on measurement hygiene

Recording from a USB drive doesn't affect the numbers — the script samples system
counters, not disk. But do close unrelated applications before recording, since the
tool measures whole-machine usage above idle and anything else running will land in
the results.
