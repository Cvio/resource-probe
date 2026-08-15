#!/usr/bin/env python3
"""
resource_probe.py - measure what an app actually costs, then predict whether
two or more apps can share one machine.

Workflow
--------
1. On machine A, start your first app, then:

       python resource_probe.py record --label sokuji --duration 120

   Exercise the app while it records. You get sokuji.json.

2. Copy the script + json to machine B, start the second app, then:

       python resource_probe.py record --label comfyui --duration 120 --baseline sokuji.json

   You get comfyui.json AND a co-residency verdict printed to screen.

3. Any time, compare any number of saved reports:

       python resource_probe.py compare sokuji.json comfyui.json ollama.json

Design notes
------------
- Samples over time and keeps PERCENTILES, not just peaks. Two apps rarely hit
  their maximum at the same instant, so naive peak-addition over-predicts
  conflict. p95 is the honest "busy" number; max is kept for the worst case.
- Measures DELTA over an idle baseline taken at startup, so you get the app's
  own cost rather than the whole machine's.
- Treats VRAM as a hard ceiling. RAM can spill to disk (slow but survivable);
  VRAM cannot. Two processes each needing 6GB on an 8GB card simply fail.
- Reports the host machine's capacity alongside the app cost, so a report taken
  on a big machine can be evaluated against a smaller target.
"""

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

try:
    import psutil
except ImportError:
    print("ERROR: psutil is required.  pip install psutil", file=sys.stderr)
    sys.exit(1)

SCHEMA_VERSION = 2


# --------------------------------------------------------------------------
# GPU support (NVIDIA via nvidia-smi; degrades gracefully if absent)
# --------------------------------------------------------------------------

def _nvidia_smi_path():
    p = shutil.which("nvidia-smi")
    if p:
        return p
    # common Windows location when not on PATH
    win = r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"
    return win if os.path.exists(win) else None


NVIDIA_SMI = _nvidia_smi_path()


def gpu_static_info():
    """One-time GPU capability info.  Returns [] if no NVIDIA GPU."""
    if not NVIDIA_SMI:
        return []
    try:
        out = subprocess.check_output(
            [NVIDIA_SMI,
             "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=10, text=True)
    except Exception:
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 2:
            try:
                gpus.append({"name": parts[0], "vram_total_mb": float(parts[1])})
            except ValueError:
                pass
    return gpus


def gpu_sample():
    """Per-sample GPU usage.  Returns list of {util_pct, vram_used_mb}."""
    if not NVIDIA_SMI:
        return []
    try:
        out = subprocess.check_output(
            [NVIDIA_SMI,
             "--query-gpu=utilization.gpu,memory.used,temperature.gpu",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=10, text=True)
    except Exception:
        return []
    samples = []
    for line in out.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 2:
            try:
                samples.append({
                    "util_pct": float(parts[0]),
                    "vram_used_mb": float(parts[1]),
                    "temp_c": float(parts[2]) if len(parts) > 2 else None,
                })
            except ValueError:
                pass
    return samples


# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------

def machine_info():
    gpus = gpu_static_info()
    vm = psutil.virtual_memory()
    freq = psutil.cpu_freq()
    return {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "cpu_logical": psutil.cpu_count(logical=True),
        "cpu_physical": psutil.cpu_count(logical=False),
        "cpu_max_mhz": round(freq.max, 0) if freq else None,
        "ram_total_mb": round(vm.total / 1024 / 1024, 1),
        "gpus": gpus,
    }


def take_sample():
    vm = psutil.virtual_memory()
    s = {
        "t": time.time(),
        "cpu_pct": psutil.cpu_percent(interval=None),
        "ram_used_mb": round((vm.total - vm.available) / 1024 / 1024, 1),
    }
    g = gpu_sample()
    if g:
        # aggregate across GPUs: sum VRAM, max utilisation
        s["vram_used_mb"] = round(sum(x["vram_used_mb"] for x in g), 1)
        s["gpu_util_pct"] = max(x["util_pct"] for x in g)
        temps = [x["temp_c"] for x in g if x.get("temp_c") is not None]
        if temps:
            s["gpu_temp_c"] = max(temps)
    return s


def pct(values, p):
    """Percentile without numpy.  p in 0..100."""
    if not values:
        return None
    vs = sorted(values)
    if len(vs) == 1:
        return vs[0]
    k = (len(vs) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(vs) - 1)
    return vs[lo] + (vs[hi] - vs[lo]) * (k - lo)


def summarise(values):
    if not values:
        return None
    return {
        "mean": round(statistics.fmean(values), 1),
        "p50": round(pct(values, 50), 1),
        "p95": round(pct(values, 95), 1),
        "max": round(max(values), 1),
        "min": round(min(values), 1),
    }


def record(label, duration, interval, baseline_path, idle_seconds, outfile):
    info = machine_info()

    print(f"Machine : {info['hostname']}  ({info['os']})")
    print(f"CPU     : {info['cpu_logical']} logical cores")
    print(f"RAM     : {info['ram_total_mb']/1024:.1f} GB")
    if info["gpus"]:
        for g in info["gpus"]:
            print(f"GPU     : {g['name']}  {g['vram_total_mb']/1024:.1f} GB VRAM")
    else:
        print("GPU     : none detected (no nvidia-smi)")
    print()

    # --- idle baseline -----------------------------------------------------
    print(f"Measuring idle baseline for {idle_seconds}s.")
    print(">>> Make sure the app you want to profile is NOT running yet. <<<")
    input("    Press Enter when ready...")
    psutil.cpu_percent(interval=None)  # prime the counter
    idle = []
    t_end = time.time() + idle_seconds
    while time.time() < t_end:
        time.sleep(interval)
        idle.append(take_sample())
    base = {
        "cpu_pct": statistics.fmean([s["cpu_pct"] for s in idle]),
        "ram_used_mb": statistics.fmean([s["ram_used_mb"] for s in idle]),
        "vram_used_mb": statistics.fmean(
            [s.get("vram_used_mb", 0) for s in idle]) if info["gpus"] else 0.0,
    }
    print(f"    idle: CPU {base['cpu_pct']:.1f}%  "
          f"RAM {base['ram_used_mb']/1024:.2f} GB  "
          f"VRAM {base['vram_used_mb']/1024:.2f} GB\n")

    # --- active recording --------------------------------------------------
    print(f"Recording '{label}' for {duration}s at {interval}s intervals.")
    print(">>> START the app now and EXERCISE it "
          "(run a real workload, not just idle). <<<")
    input("    Press Enter to begin recording...")

    samples = []
    t_end = time.time() + duration
    last_print = 0
    while time.time() < t_end:
        time.sleep(interval)
        samples.append(take_sample())
        remaining = int(t_end - time.time())
        if remaining != last_print and remaining % 10 == 0:
            s = samples[-1]
            print(f"    {remaining:4d}s left | CPU {s['cpu_pct']:5.1f}% | "
                  f"RAM {s['ram_used_mb']/1024:5.2f} GB"
                  + (f" | VRAM {s.get('vram_used_mb',0)/1024:5.2f} GB"
                     f" | GPU {s.get('gpu_util_pct',0):3.0f}%" if info["gpus"] else ""))
            last_print = remaining

    if not samples:
        print("No samples collected.", file=sys.stderr)
        sys.exit(1)

    # --- build report ------------------------------------------------------
    def deltas(key, base_key):
        return [max(0.0, s.get(key, 0) - base[base_key]) for s in samples]

    report = {
        "schema_version": SCHEMA_VERSION,
        "label": label,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": duration,
        "sample_interval_s": interval,
        "sample_count": len(samples),
        "machine": info,
        "idle_baseline": {
            "cpu_pct": round(base["cpu_pct"], 1),
            "ram_used_mb": round(base["ram_used_mb"], 1),
            "vram_used_mb": round(base["vram_used_mb"], 1),
        },
        # absolute = whole machine while app ran
        "absolute": {
            "cpu_pct": summarise([s["cpu_pct"] for s in samples]),
            "ram_used_mb": summarise([s["ram_used_mb"] for s in samples]),
            "vram_used_mb": summarise([s.get("vram_used_mb", 0) for s in samples])
                            if info["gpus"] else None,
            "gpu_util_pct": summarise([s.get("gpu_util_pct", 0) for s in samples])
                            if info["gpus"] else None,
            "gpu_temp_c": summarise([s["gpu_temp_c"] for s in samples
                                     if s.get("gpu_temp_c") is not None])
                          if info["gpus"] else None,
        },
        # cost = app's own footprint above idle  <- this is what we add up
        "cost": {
            "cpu_pct": summarise(deltas("cpu_pct", "cpu_pct")),
            "ram_mb": summarise(deltas("ram_used_mb", "ram_used_mb")),
            "vram_mb": summarise(deltas("vram_used_mb", "vram_used_mb"))
                       if info["gpus"] else None,
        },
        # raw series, normalised to t=0, for charting
        "series": {
            "t": [round(s["t"] - samples[0]["t"], 1) for s in samples],
            "cpu_pct": [round(s["cpu_pct"], 1) for s in samples],
            "ram_mb": [round(max(0.0, s["ram_used_mb"] - base["ram_used_mb"]), 1)
                       for s in samples],
            "vram_mb": [round(max(0.0, s.get("vram_used_mb", 0) - base["vram_used_mb"]), 1)
                        for s in samples] if info["gpus"] else None,
            "gpu_util_pct": [round(s.get("gpu_util_pct", 0), 1) for s in samples]
                            if info["gpus"] else None,
        },
    }

    with open(outfile, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nSaved -> {outfile}")
    print_cost(report)

    if baseline_path:
        with open(baseline_path) as f:
            prior = json.load(f)
        print()
        verdict(([prior] if isinstance(prior, dict) else prior) + [report], info)

    return report


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def fmt_gb(mb):
    return f"{mb/1024:.2f} GB" if mb is not None else "n/a"


def print_cost(r):
    c = r["cost"]
    print(f"\n--- cost of '{r['label']}' (above idle) ---")
    print(f"  CPU   p95 {c['cpu_pct']['p95']:5.1f}%   max {c['cpu_pct']['max']:5.1f}%")
    print(f"  RAM   p95 {fmt_gb(c['ram_mb']['p95'])}   max {fmt_gb(c['ram_mb']['max'])}")
    if c.get("vram_mb"):
        print(f"  VRAM  p95 {fmt_gb(c['vram_mb']['p95'])}   max {fmt_gb(c['vram_mb']['max'])}")


def compute_verdict(reports, target_machine=None):
    """Analyse whether reports can co-reside.  Returns structured result dict."""
    if target_machine is None:
        target_machine = reports[-1]["machine"]

    cpu_p95 = sum(r["cost"]["cpu_pct"]["p95"] for r in reports)
    cpu_max = sum(r["cost"]["cpu_pct"]["max"] for r in reports)
    ram_p95 = sum(r["cost"]["ram_mb"]["p95"] for r in reports)
    ram_max = sum(r["cost"]["ram_mb"]["max"] for r in reports)

    vram_reports = [r for r in reports if r["cost"].get("vram_mb")]
    vram_p95 = sum(r["cost"]["vram_mb"]["p95"] for r in vram_reports)
    vram_max = sum(r["cost"]["vram_mb"]["max"] for r in vram_reports)

    ram_total = target_machine["ram_total_mb"]
    ram_reserve = max(2048, ram_total * 0.10)
    ram_avail = ram_total - ram_reserve

    vram_total = sum(g["vram_total_mb"] for g in target_machine["gpus"]) \
                 if target_machine["gpus"] else 0
    vram_reserve = max(512, vram_total * 0.10) if vram_total else 0
    vram_avail = vram_total - vram_reserve

    issues, warnings, notes = [], [], []
    res = {
        "labels": [r["label"] for r in reports],
        "target": target_machine,
        "ram": {"p95": ram_p95, "max": ram_max, "avail": ram_avail,
                "total": ram_total, "status": None},
        "vram": {"p95": vram_p95, "max": vram_max, "avail": vram_avail,
                 "total": vram_total, "status": None,
                 "applicable": bool(vram_reports)},
        "cpu": {"p95": cpu_p95, "max": cpu_max, "status": None},
    }

    # RAM: soft ceiling, can swap
    if ram_p95 > ram_avail:
        res["ram"]["status"] = "fail"
        issues.append(f"RAM short by {fmt_gb(ram_p95 - ram_avail)} at typical load")
    elif ram_max > ram_avail:
        res["ram"]["status"] = "warn"
        warnings.append(f"RAM peaks exceed available by {fmt_gb(ram_max - ram_avail)}")
    else:
        res["ram"]["status"] = "ok"

    # VRAM: hard ceiling, cannot swap
    if vram_reports:
        if not target_machine["gpus"]:
            res["vram"]["status"] = "fail"
            issues.append("apps need VRAM but target machine has no GPU")
        elif vram_p95 > vram_avail:
            res["vram"]["status"] = "fail"
            issues.append(f"VRAM short by {fmt_gb(vram_p95 - vram_avail)} "
                          f"(hard limit - cannot swap)")
        elif vram_max > vram_avail:
            res["vram"]["status"] = "warn"
            warnings.append(f"VRAM peaks exceed by {fmt_gb(vram_max - vram_avail)}")
        else:
            res["vram"]["status"] = "ok"
        if len(vram_reports) > 1:
            notes.append("Multiple GPU apps: consider staggering model loads, "
                         "or set per-app VRAM limits if the runtime supports it.")

    # CPU: elastic, contention slows rather than kills
    if cpu_p95 > 100:
        res["cpu"]["status"] = "warn"
        warnings.append(f"CPU oversubscribed at typical load ({cpu_p95:.0f}%)")
    else:
        res["cpu"]["status"] = "ok"

    temps = [r["absolute"].get("gpu_temp_c") for r in reports
             if r["absolute"].get("gpu_temp_c")]
    if temps and max(t["max"] for t in temps) > 80:
        warnings.append("GPU already runs hot alone; combined load may throttle")

    if issues:
        res["verdict"] = "fail"
        res["headline"] = "WILL NOT RUN TOGETHER"
        opts = []
        if res["vram"]["status"] == "fail":
            opts += ["Use smaller or quantised models to cut VRAM",
                     "Run one app on CPU (slower, but frees the GPU)",
                     "Keep them on separate machines"]
        if res["ram"]["status"] == "fail":
            opts.append(f"Add RAM: {fmt_gb(ram_p95 + ram_reserve)} total "
                        f"would be comfortable")
        res["options"] = opts
    elif warnings:
        res["verdict"] = "warn"
        res["headline"] = "PROBABLY OK, WITH CAVEATS"
        res["options"] = ["Peaks rarely coincide, so this usually works in practice.",
                          "Watch for stutter when both apps are busy at once."]
    else:
        res["verdict"] = "ok"
        res["headline"] = "SHOULD RUN TOGETHER COMFORTABLY"
        res["options"] = []

    res["issues"] = issues
    res["warnings"] = warnings
    res["notes"] = notes
    return res


def verdict(reports, target_machine=None):
    """Predict whether all reports can run together on target_machine."""
    v = compute_verdict(reports, target_machine)
    tm = v["target"]

    print("=" * 62)
    print(f"CO-RESIDENCY CHECK: {' + '.join(v['labels'])}")
    print(f"Target machine: {tm['hostname']}  "
          f"({tm['cpu_logical']} cores, "
          f"{tm['ram_total_mb']/1024:.1f} GB RAM", end="")
    if tm["gpus"]:
        print(f", {sum(g['vram_total_mb'] for g in tm['gpus'])/1024:.1f} GB VRAM)")
    else:
        print(", no GPU)")
    print("=" * 62)

    tag = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}

    r = v["ram"]
    print(f"\nRAM    need p95 {fmt_gb(r['p95'])} / worst {fmt_gb(r['max'])}"
          f"  vs usable {fmt_gb(r['avail'])}")
    if r["status"] == "fail":
        print("       FAIL - typical combined load exceeds available RAM")
    elif r["status"] == "warn":
        print("       WARN - fits typically, but simultaneous peaks would swap")
    else:
        print(f"       OK   - {fmt_gb(r['avail'] - r['max'])} spare even at peak")

    g = v["vram"]
    if g["applicable"]:
        if not tm["gpus"]:
            print("\nVRAM   FAIL - GPU workloads but no GPU on target")
        else:
            print(f"\nVRAM   need p95 {fmt_gb(g['p95'])} / worst {fmt_gb(g['max'])}"
                  f"  vs usable {fmt_gb(g['avail'])}")
            if g["status"] == "fail":
                print("       FAIL - VRAM cannot overflow to disk; this will OOM")
            elif g["status"] == "warn":
                print("       WARN - peak collision would OOM one of the apps")
            else:
                print(f"       OK   - {fmt_gb(g['avail'] - g['max'])} spare even at peak")

    c = v["cpu"]
    print(f"\nCPU    need p95 {c['p95']:.0f}% / worst {c['max']:.0f}%  vs 100% available")
    if c["status"] == "warn":
        print("       WARN - sustained contention; both apps will run slower")
    elif c["max"] > 100:
        print("       OK   - brief peak contention only, self-resolving")
    else:
        print(f"       OK   - {100 - c['max']:.0f}% headroom even at peak")

    for n in v["notes"]:
        print(f"       note: {n}")

    print("\n" + "-" * 62)
    print(f"VERDICT: {v['headline']}")
    for i in v["issues"]:
        print(f"  - {i}")
    for w in v["warnings"]:
        print(f"  - {w}")
    if v["options"]:
        print()
        for o in v["options"]:
            print(f"  * {o}")
    if v["verdict"] == "ok":
        print(f"  Spare at peak: RAM {fmt_gb(v['ram']['avail'] - v['ram']['max'])}", end="")
        if g["applicable"] and g["total"]:
            print(f", VRAM {fmt_gb(g['avail'] - g['max'])}", end="")
        print(f", CPU {100 - c['max']:.0f}%")
    print("-" * 62)
    return v


def _svg_chart(series_list, title, unit, height=170, width=680):
    """Self-contained inline SVG line chart.  series_list: [(label, ts, vals, colour)]"""
    valid = [(l, t, v, c) for (l, t, v, c) in series_list if v and any(x is not None for x in v)]
    if not valid:
        return ""
    all_vals = [x for (_, _, v, _) in valid for x in v if x is not None]
    all_ts = [x for (_, t, _, _) in valid for x in t]
    if not all_vals or not all_ts:
        return ""
    vmax = max(all_vals) or 1.0
    vmax *= 1.15
    tmax = max(all_ts) or 1.0
    pl, pr, pt, pb = 58, 12, 14, 26
    iw, ih = width - pl - pr, height - pt - pb

    def X(t): return pl + (t / tmax) * iw
    def Y(v): return pt + ih - (v / vmax) * ih

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" '
             f'preserveAspectRatio="xMidYMid meet">']
    # gridlines + y labels
    for i in range(5):
        v = vmax * i / 4
        y = Y(v)
        parts.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{width-pr}" y2="{y:.1f}" '
                     f'class="grid"/>')
        lbl = f"{v/1024:.1f}" if unit == "GB" else f"{v:.0f}"
        parts.append(f'<text x="{pl-7}" y="{y+3.5:.1f}" class="ylab">{lbl}</text>')
    # x labels
    for i in range(5):
        t = tmax * i / 4
        parts.append(f'<text x="{X(t):.1f}" y="{height-8}" class="xlab">{t:.0f}s</text>')
    # lines
    for (label, ts, vals, colour) in valid:
        pts = " ".join(f"{X(t):.1f},{Y(v):.1f}"
                       for t, v in zip(ts, vals) if v is not None)
        if pts:
            parts.append(f'<polyline points="{pts}" fill="none" stroke="{colour}" '
                         f'stroke-width="2" stroke-linejoin="round"/>')
    parts.append('</svg>')
    legend = " ".join(
        f'<span class="key"><i style="background:{c}"></i>{esc(l)}</span>'
        for (l, _, _, c) in valid)
    yunit = "GB" if unit == "GB" else unit
    return (f'<div class="chartbox"><div class="charthead">{esc(title)} '
            f'<span class="unit">({yunit})</span></div>'
            f'{"".join(parts)}<div class="legend">{legend}</div></div>')


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


PALETTE = ["#2f81f7", "#e3654a", "#3fb950", "#a371f7", "#d29922", "#1f9c9c"]


def build_html(reports, v):
    tm = v["target"]
    vd = v["verdict"]
    colours = {r["label"]: PALETTE[i % len(PALETTE)] for i, r in enumerate(reports)}

    # charts
    charts = []
    charts.append(_svg_chart(
        [(r["label"], r["series"]["t"], r["series"]["cpu_pct"], colours[r["label"]])
         for r in reports if r.get("series")],
        "CPU usage over time", "%"))
    charts.append(_svg_chart(
        [(r["label"], r["series"]["t"], r["series"]["ram_mb"], colours[r["label"]])
         for r in reports if r.get("series")],
        "RAM above idle, over time", "GB"))
    gpu_series = [(r["label"], r["series"]["t"], r["series"].get("vram_mb"),
                   colours[r["label"]])
                  for r in reports if r.get("series") and r["series"].get("vram_mb")]
    if gpu_series:
        charts.append(_svg_chart(gpu_series, "VRAM above idle, over time", "GB"))
    util_series = [(r["label"], r["series"]["t"], r["series"].get("gpu_util_pct"),
                    colours[r["label"]])
                   for r in reports if r.get("series") and r["series"].get("gpu_util_pct")]
    if util_series:
        charts.append(_svg_chart(util_series, "GPU utilisation over time", "%"))

    # per-app cards
    cards = []
    for r in reports:
        c = r["cost"]
        rows = [("CPU", f"{c['cpu_pct']['p95']:.0f}%", f"{c['cpu_pct']['max']:.0f}%"),
                ("RAM", fmt_gb(c["ram_mb"]["p95"]), fmt_gb(c["ram_mb"]["max"]))]
        if c.get("vram_mb"):
            rows.append(("VRAM", fmt_gb(c["vram_mb"]["p95"]), fmt_gb(c["vram_mb"]["max"])))
        body = "".join(f"<tr><th>{k}</th><td>{p}</td><td>{m}</td></tr>"
                       for k, p, m in rows)
        mach = r["machine"]
        cards.append(f"""
      <div class="card">
        <h3><i class="dot" style="background:{colours[r['label']]}"></i>{esc(r['label'])}</h3>
        <table class="mini">
          <tr><th></th><td class="hd">typical</td><td class="hd">peak</td></tr>
          {body}
        </table>
        <p class="meta">measured on {esc(mach['hostname'])} &middot;
           {r['sample_count']} samples over {r['duration_s']}s</p>
      </div>""")

    # resource bars
    def bar(name, need_p95, need_max, avail, total, status, hard):
        if not total:
            return ""
        p95w = min(100, need_p95 / total * 100)
        maxw = min(100, need_max / total * 100)
        availw = avail / total * 100
        note = "hard limit &mdash; cannot overflow to disk" if hard \
               else "can overflow to disk (slowly)"
        return f"""
      <div class="res {status}">
        <div class="resline"><b>{name}</b>
          <span class="need">need {fmt_gb(need_p95) if total > 200 else f'{need_p95:.0f}%'}
          typical / {fmt_gb(need_max) if total > 200 else f'{need_max:.0f}%'} peak</span>
          <span class="cap">of {fmt_gb(total) if total > 200 else '100%'}</span>
        </div>
        <div class="track">
          <div class="fill-max" style="width:{maxw:.1f}%"></div>
          <div class="fill-p95" style="width:{p95w:.1f}%"></div>
          <div class="limit" style="left:{availw:.1f}%"><span>usable</span></div>
        </div>
        <div class="resnote">{note}</div>
      </div>"""

    bars = bar("RAM", v["ram"]["p95"], v["ram"]["max"], v["ram"]["avail"],
               v["ram"]["total"], v["ram"]["status"], False)
    if v["vram"]["applicable"] and v["vram"]["total"]:
        bars += bar("VRAM", v["vram"]["p95"], v["vram"]["max"], v["vram"]["avail"],
                    v["vram"]["total"], v["vram"]["status"], True)
    bars += bar("CPU", v["cpu"]["p95"], v["cpu"]["max"], 100, 100,
                v["cpu"]["status"], False)

    bullets = "".join(f"<li>{esc(x)}</li>" for x in v["issues"] + v["warnings"])
    opts = "".join(f"<li>{esc(x)}</li>" for x in v["options"])
    notes = "".join(f"<li>{esc(x)}</li>" for x in v["notes"])

    gpu_desc = ", ".join(f"{esc(g['name'])} ({g['vram_total_mb']/1024:.1f} GB)"
                         for g in tm["gpus"]) or "no GPU"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Resource report: {esc(' + '.join(v['labels']))}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#0d1117;color:#e6edf3;
 font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:860px;margin:0 auto;padding:34px 22px 70px}}
h1{{font-size:24px;margin:0 0 4px;letter-spacing:-.3px}}
.sub{{color:#8b949e;font-size:13px;margin-bottom:26px}}
.verdict{{border-radius:10px;padding:20px 22px;margin-bottom:26px;border:1px solid}}
.verdict.ok{{background:#0d2a16;border-color:#2ea04326}}
.verdict.warn{{background:#2b2611;border-color:#d2992226}}
.verdict.fail{{background:#2d1416;border-color:#f8514926}}
.verdict h2{{margin:0 0 6px;font-size:19px}}
.verdict.ok h2{{color:#3fb950}} .verdict.warn h2{{color:#d29922}}
.verdict.fail h2{{color:#f85149}}
.verdict ul{{margin:10px 0 0;padding-left:20px;color:#c9d1d9}}
.verdict li{{margin:3px 0}}
.verdict .opts{{margin-top:14px;padding-top:12px;border-top:1px solid #ffffff14}}
.verdict .opts b{{font-size:12px;text-transform:uppercase;letter-spacing:.6px;
 color:#8b949e;display:block;margin-bottom:6px}}
h2.sec{{font-size:13px;text-transform:uppercase;letter-spacing:.8px;color:#8b949e;
 margin:32px 0 12px;font-weight:600}}
.res{{margin-bottom:20px}}
.resline{{display:flex;align-items:baseline;gap:10px;margin-bottom:6px;font-size:13px}}
.resline b{{font-size:14px;min-width:46px}}
.need{{color:#c9d1d9}} .cap{{color:#6e7681;margin-left:auto}}
.track{{position:relative;height:26px;background:#161b22;border-radius:5px;
 overflow:hidden;border:1px solid #30363d}}
.fill-max{{position:absolute;top:0;bottom:0;left:0;background:#30363d}}
.fill-p95{{position:absolute;top:0;bottom:0;left:0}}
.ok .fill-p95{{background:#2ea043}} .warn .fill-p95{{background:#d29922}}
.fail .fill-p95{{background:#da3633}}
.limit{{position:absolute;top:0;bottom:0;width:2px;background:#e6edf3}}
.limit span{{position:absolute;top:-1px;left:5px;font-size:10px;color:#8b949e;
 white-space:nowrap}}
.resnote{{font-size:11px;color:#6e7681;margin-top:4px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:14px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:9px;padding:15px 17px}}
.card h3{{margin:0 0 10px;font-size:15px;display:flex;align-items:center;gap:8px}}
.dot{{width:9px;height:9px;border-radius:50%;display:inline-block;flex:none}}
table.mini{{width:100%;border-collapse:collapse;font-size:13px}}
table.mini th{{text-align:left;color:#8b949e;font-weight:500;padding:3px 0}}
table.mini td{{text-align:right;padding:3px 0;font-variant-numeric:tabular-nums}}
td.hd{{color:#6e7681;font-size:11px;text-transform:uppercase;letter-spacing:.5px}}
.meta{{margin:11px 0 0;font-size:11px;color:#6e7681}}
.chartbox{{background:#161b22;border:1px solid #30363d;border-radius:9px;
 padding:14px 16px 10px;margin-bottom:16px}}
.charthead{{font-size:13px;color:#c9d1d9;margin-bottom:6px}}
.unit{{color:#6e7681}}
.chart{{width:100%;height:auto;display:block}}
.grid{{stroke:#30363d;stroke-width:1}}
.ylab{{fill:#6e7681;font-size:10px;text-anchor:end}}
.xlab{{fill:#6e7681;font-size:10px;text-anchor:middle}}
.legend{{display:flex;gap:14px;flex-wrap:wrap;margin-top:6px}}
.key{{font-size:11px;color:#8b949e;display:flex;align-items:center;gap:5px}}
.key i{{width:11px;height:3px;border-radius:2px;display:inline-block}}
.machine{{background:#161b22;border:1px solid #30363d;border-radius:9px;
 padding:14px 17px;font-size:13px;color:#c9d1d9}}
.machine b{{color:#e6edf3}}
footer{{margin-top:40px;font-size:11px;color:#6e7681;border-top:1px solid #21262d;
 padding-top:14px}}
</style></head><body><div class="wrap">

<h1>Can these run together?</h1>
<div class="sub">{esc(' + '.join(v['labels']))} &middot; generated
 {esc(datetime.now().strftime('%Y-%m-%d %H:%M'))}</div>

<div class="verdict {vd}">
  <h2>{esc(v['headline'])}</h2>
  {'<ul>' + bullets + '</ul>' if bullets else
   '<div style="color:#c9d1d9">Every resource has headroom at peak load.</div>'}
  {'<div class="opts"><b>' + ('What to do' if vd=='fail' else 'Notes') + '</b><ul>'
   + opts + notes + '</ul></div>' if (opts or notes) else ''}
</div>

<h2 class="sec">Combined demand vs capacity</h2>
{bars}

<h2 class="sec">Cost per app <span style="text-transform:none;letter-spacing:0">
 (above idle baseline)</span></h2>
<div class="cards">{''.join(cards)}</div>

<h2 class="sec">Behaviour over time</h2>
{''.join(charts)}

<h2 class="sec">Target machine</h2>
<div class="machine">
  <b>{esc(tm['hostname'])}</b> &middot; {esc(tm['os'])}<br>
  {tm['cpu_logical']} logical cores &middot;
  {tm['ram_total_mb']/1024:.1f} GB RAM &middot; {gpu_desc}
</div>

<footer>
  <b>typical</b> = 95th percentile, the honest "both apps busy" number.
  <b>peak</b> = absolute maximum observed.<br>
  Peaks from different apps rarely coincide, so summing peaks over-predicts
  conflict; summing typicals is the better guide.
  RAM can overflow to disk and merely slow down &mdash; VRAM cannot, and will
  fail outright.
  Usable capacity reserves 10% (min 2 GB RAM / 512 MB VRAM) for the operating
  system.
</footer>
</div></body></html>"""


def write_html(reports, v, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_html(reports, v))
    return path


def main():
    ap = argparse.ArgumentParser(
        description="Measure app resource cost and predict co-residency.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="profile one app on this machine")
    rec.add_argument("--label", required=True, help="name of the app, e.g. sokuji")
    rec.add_argument("--duration", type=int, default=120, help="seconds to record")
    rec.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    rec.add_argument("--idle-seconds", type=int, default=15,
                     help="seconds of idle baseline before recording")
    rec.add_argument("--baseline", help="previous report json to compare against")
    rec.add_argument("--out", help="output file (default: <label>.json)")
    rec.add_argument("--html", nargs="?", const="AUTO", default=None,
                     help="also write an HTML report (optional filename)")

    cmp_ = sub.add_parser("compare", help="compare saved reports")
    cmp_.add_argument("reports", nargs="+", help="two or more report json files")
    cmp_.add_argument("--target", help="report whose machine is the target "
                                       "(default: this machine)")
    cmp_.add_argument("--html", nargs="?", const="AUTO", default=None,
                      help="write an HTML report (optional filename)")

    a = ap.parse_args()

    if a.cmd == "record":
        out = a.out or f"{a.label}.json"
        rep = record(a.label, a.duration, a.interval, a.baseline,
                     a.idle_seconds, out)
        if a.html:
            reports = [rep]
            if a.baseline:
                with open(a.baseline) as f:
                    prior = json.load(f)
                reports = ([prior] if isinstance(prior, dict) else prior) + [rep]
            v = compute_verdict(reports)
            name = (f"{'-'.join(r['label'] for r in reports)}.html"
                    if a.html == "AUTO" else a.html)
            write_html(reports, v, name)
            print(f"\nHTML report -> {os.path.abspath(name)}")
    else:
        reports = []
        for p in a.reports:
            with open(p) as f:
                reports.append(json.load(f))
        for r in reports:
            print_cost(r)
        print()
        if a.target:
            with open(a.target) as f:
                tgt = json.load(f)["machine"]
        else:
            tgt = machine_info()
        v = verdict(reports, tgt)
        if a.html:
            name = (f"{'-'.join(r['label'] for r in reports)}.html"
                    if a.html == "AUTO" else a.html)
            write_html(reports, v, name)
            print(f"\nHTML report -> {os.path.abspath(name)}")


if __name__ == "__main__":
    main()
