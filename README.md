# resource-probe

Measure what an application actually costs in CPU, RAM, and VRAM — then find out
whether two or more of them can share one machine.

Built for a specific problem: several apps, each currently running alone on its
own machine, and the question of whether they could be consolidated. Guessing from
model sizes and task-manager glances doesn't answer it. This does.

## What it does

1. **Records** an app's resource cost on one machine, sampling over time.
2. **Carries** that measurement as a JSON file to the next machine.
3. **Predicts** whether the apps can co-reside, with a verdict and reasons.
4. **Reports** the result as a self-contained HTML page with charts.

No admin rights, no drivers, no install on the machines being measured.

## Quick start

There are two ways to run this, and which you want depends on whether you're
allowed to install things on the machines you're measuring.

- **Installing is fine** — use the Python route directly below.
- **Installing is not fine** (measuring machines that aren't yours, or you want
  one portable USB tool) — skip to [Portable build](#portable-build-no-python-on-the-target).

### Python route

```bash
pip install psutil
```

On machine A, with the first app ready to run:

```bash
python resource_probe.py record --label sokuji --duration 120
```

It measures an idle baseline first (it will prompt you), then records while you
**exercise the app** — run a real workload, not just leave it sitting open. An idle
app tells you nothing useful.

Copy the script and `sokuji.json` to machine B, then:

```bash
python resource_probe.py record --label comfyui --duration 120 --baseline sokuji.json --html
```

You get `comfyui.json`, a verdict in the terminal, and an HTML report.

Compare any saved reports at any time:

```bash
python resource_probe.py compare sokuji.json comfyui.json ollama.json --html
```

---

## Portable build: no Python on the target

To measure machines without installing anything on them, build a standalone
executable once on your own machine and carry that instead.

### Build it (one time, on a machine you control)

On Windows, just run:

```bat
build-exe.bat
```

Or do it by hand on any platform:

```bash
pip install pyinstaller psutil
pyinstaller --onefile --name resource_probe resource_probe.py
```

Either produces `dist/resource_probe.exe` — roughly 10 MB, with a Python
interpreter and psutil bundled inside. Copy that single file to your USB drive.

### Run it anywhere

```bat
E:\resource_probe.exe record --label sokuji --duration 120
E:\resource_probe.exe record --label comfyui --baseline sokuji.json --html
```

No Python, no pip, no admin rights, nothing left behind on the host.

### Write reports to the drive, not wherever you launched from

By default reports land in the current working directory. Be explicit if you want
them on the USB:

```bat
E:\resource_probe.exe record --label sokuji ^
  --out E:\reports\sokuji.json ^
  --html E:\reports\sokuji.html
```

### Do not use a virtual environment for this

A venv on a USB drive will not work on other machines. A venv is not
self-contained: its `pyvenv.cfg` hardcodes an absolute path to the base Python
installation that created it. On any machine where that path doesn't exist — or
points at a different Python version — the venv fails. Venvs isolate projects on
*one* machine; they don't travel.

The same problem rules out simply copying `psutil` onto the drive. psutil is a
compiled C extension, so a build for Python 3.12 x64 will not load under 3.11 or
on ARM. The executable avoids both issues by bundling a matching interpreter with
the library.

### If you'd rather keep it as a script

Download the **Windows embeddable package** from python.org, unzip it onto the
drive, and install psutil beside it:

```bat
pip install --target=E:\python\Lib\site-packages psutil
E:\python\python.exe E:\resource_probe.py record --label sokuji
```

Self-contained and installs nothing on the host, but it's more moving parts than
the single executable.

### Caveats for the portable build

- The executable is **OS- and architecture-specific**. Build separately on macOS or
  Linux if you need those.
- Some antivirus software flags PyInstaller binaries as suspicious. It's a known
  false-positive pattern, but worth knowing before you hand the drive to someone
  else.
- Startup is slightly slower than a plain script — the exe unpacks itself to a temp
  directory on each run. Irrelevant for a tool that then samples for two minutes.

---

## Why the numbers are trustworthy

Three decisions do the work here.

**It measures cost above idle, not absolute usage.** Each recording starts with an
idle baseline and subtracts it. What you get is the app's own footprint rather than
"whatever this machine happened to be doing," which is what makes measurements from
different machines addable at all.

**It keeps the 95th percentile alongside the maximum.** Adding two peaks
over-predicts conflict, because peaks from different apps rarely coincide. The p95
is the honest "both apps are busy" number. The verdict distinguishes *fails at
typical load* (a real problem) from *fails only if peaks collide* (usually fine in
practice).

**It treats VRAM as a hard ceiling, separate from RAM.** RAM overcommit means
swapping — slow but survivable. VRAM overcommit means the application crashes.
Collapsing them into one "memory" number hides the difference that matters most.

## Reading the output

| Term | Meaning |
|---|---|
| **typical** | 95th percentile — the sustained busy level |
| **peak** | absolute maximum observed |
| **usable** | total capacity minus a reserve for the OS (10%, min 2 GB RAM / 512 MB VRAM) |

Verdicts are one of three:

- **Should run together comfortably** — headroom at peak on every resource.
- **Probably OK, with caveats** — fits at typical load; peaks would collide.
- **Will not run together** — typical combined load exceeds a hard limit.

CPU contention is reported as a warning rather than a failure: oversubscribed CPU
makes things slower, it doesn't make them crash.

## Options

```
record --label NAME          name for this app (required)
       --duration N          seconds to record (default 120)
       --interval N          seconds between samples (default 1.0)
       --idle-seconds N      idle baseline length (default 15)
       --baseline FILE.json  compare against a previous report
       --out FILE.json       where to save (default <label>.json)
       --html [FILE.html]    also write an HTML report

compare FILE.json ...        two or more reports
       --target FILE.json    use that report's machine as the target
                             (default: the machine you're running on)
       --html [FILE.html]    write an HTML report
```

`--target` matters when you want to know whether apps measured on big machines
would fit on a smaller one.

## GPU support

GPU metrics come from `nvidia-smi`, which ships with the NVIDIA driver — nothing
extra to install. On machines without an NVIDIA GPU the script skips GPU metrics
and still reports CPU and RAM. Multi-GPU machines have their VRAM summed and
utilisation taken as the maximum across cards.

AMD and Intel GPUs are not currently supported.

## Running from a USB drive

Works fine — no admin, no drivers, reports write next to the script. See
[Portable build](#portable-build-no-python-on-the-target) above for the
recommended approach, or [USB-SETUP.md](USB-SETUP.md) for additional detail and
alternatives.

## Limitations

- **Whole-machine sampling.** It measures total system usage minus idle, not a
  specific process tree. If something else starts up mid-recording, it lands in the
  numbers. Close what you're not measuring.
- **NVIDIA only** for GPU metrics.
- **Prediction, not proof.** Summed measurements are a well-founded estimate.
  Memory fragmentation, driver overhead, and allocator behaviour can still surprise
  you. Treat a marginal pass as marginal.
- **Charts are static SVG.** No hover, no zoom — a deliberate trade to keep the
  HTML fully self-contained and offline-capable.

## Requirements

Python 3.8+ and `psutil`. Nothing else.
