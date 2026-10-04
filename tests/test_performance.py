#!/usr/bin/env python3
"""
Lumi LLM Benchmark Suite (publication-grade)
============================================
Measures, for a local Ollama model:
  * Latency decomposition : TTFT, TPOT, per-token ITL (pooled), E2E, model-load, prompt-eval
  * Throughput            : client-side (streamed) and server-side (Ollama eval_count/eval_duration)
  * Hardware telemetry    : sampled DURING inference in a background thread (GPU util, VRAM, power,
                            temperature, SM clock, CPU, Ollama RSS) + idle baseline + energy per token
  * Persona quality       : LLM-as-a-judge (rubric-anchored G-Eval style, JSON output, multi-sample)
                            + deterministic checks (bullets, assistant tropes, sentence burstiness, Distinct-n)
  * Statistics            : mean +/- 95% CI (t), median with bootstrap CI, IQR, P95/P99, Wilson CIs for rates
  * Reproducibility       : fixed seeds, shuffled schedule, full metadata (GPU, driver, Ollama, model digest)

Outputs (results/lumi_bench_<timestamp>/):
  metadata.json, requests.csv, itl_tokens.csv, telemetry.csv, judge.csv, responses.jsonl,
  summary.csv / summary.md / summary.tex, dashboard.png/.pdf, figures/*.pdf + *.png

Usage:
  python test_performance.py
  python test_performance.py --reps 30 --judge-model llama3.1:8b-instruct-q4_K_M
"""
import argparse
import json
import os
import platform
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psutil
import requests
from scipy import stats

# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------
DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b-instruct-q4_K_M"

SYSTEM_PROMPT = (
    "You are Lumi, a hyper-expressive, casual AI companion and dueling copilot. "
    "CRITICAL RULE: Never sound like an AI assistant. Use conversational fragments, "
    "natural pauses, varied sentence length, and zero bullet points or corporate phrasing."
)

TEST_PROMPTS = [
    "Hey Lumi, I just bricked my opening hand entirely. What do I do?",
    "Wait, did you see that last play? Talk to me honestly.",
    "Can you explain why my backrow setup got completely dismantled?",
    "Quick, give me a casual summary of what we should adjust next.",
    "Are you sure that combo line works against Tenpai? Seems risky.",
    "Stop overthinking it and just tell me if we go first or second.",
]

WARMUP_RUNS = 3
IDLE_BASELINE_SEC = 3.0
BOOTSTRAP_RESAMPLES = 2000
SAMPLER_INTERVAL = 0.05  # 20 Hz telemetry

# Okabe-Ito colour-blind-safe palette
BLUE, VERM, GREEN, ORANGE, PURPLE, SKY, GREY = (
    "#0072B2", "#D55E00", "#009E73", "#E69F00", "#CC79A7", "#56B4E9", "#7F7F7F")

TROPE_PATTERNS = [
    r"\bas an ai\b", r"\bi'?d be happy to\b", r"\bcertainly\b", r"\bof course\b",
    r"\bi hope this helps\b", r"\bfeel free to\b", r"\blet me know if\b",
    r"\bin summary\b", r"\bgreat question\b", r"\bhere (?:is|are)\b",
    r"\bi understand that\b", r"\bit'?s important to\b",
]
BULLET_RE = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", re.MULTILINE)

# ---------------------------------------------------------------------------
# 2. Hardware telemetry (background sampler: measures DURING inference)
# ---------------------------------------------------------------------------
HAS_NVML = False
GPU = None
try:
    import pynvml
    pynvml.nvmlInit()
    GPU = pynvml.nvmlDeviceGetHandleByIndex(0)
    HAS_NVML = True
except Exception as exc:  # noqa: BLE001
    print(f"[Warning] NVML unavailable, GPU metrics will be NaN: {exc}")


def _nvml(fn, *a, scale=1.0):
    try:
        return float(fn(*a)) / scale
    except Exception:  # noqa: BLE001
        return float("nan")


class TelemetrySampler(threading.Thread):
    """Samples CPU / GPU / VRAM / power / temp / clocks at a fixed rate on a monotonic clock."""

    def __init__(self, interval=SAMPLER_INTERVAL):
        super().__init__(daemon=True)
        self.interval = interval
        self._halt = threading.Event()
        self.rows = []
        self._procs = []

    def _refresh_procs(self):
        self._procs = [p for p in psutil.process_iter(["name"])
                       if "ollama" in (p.info["name"] or "").lower()]

    def _rss_mb(self):
        total = 0
        for p in self._procs:
            try:
                total += p.memory_info().rss
            except psutil.Error:
                pass
        return total / (1024 * 1024)

    def run(self):
        psutil.cpu_percent(interval=None)  # prime (first call always returns 0)
        self._refresh_procs()
        tick = 0
        while not self._halt.is_set():
            t = time.perf_counter()
            if tick % 40 == 0:
                self._refresh_procs()
            row = {"t": t, "cpu_pct": psutil.cpu_percent(interval=None), "ollama_rss_mb": self._rss_mb(),
                   "gpu_util_pct": float("nan"), "vram_mb": float("nan"), "power_w": float("nan"),
                   "temp_c": float("nan"), "sm_clock_mhz": float("nan")}
            if HAS_NVML:
                try:
                    row["gpu_util_pct"] = float(pynvml.nvmlDeviceGetUtilizationRates(GPU).gpu)
                    row["vram_mb"] = pynvml.nvmlDeviceGetMemoryInfo(GPU).used / (1024 * 1024)
                except Exception:  # noqa: BLE001
                    pass
                row["power_w"] = _nvml(pynvml.nvmlDeviceGetPowerUsage, GPU, scale=1000.0)
                row["temp_c"] = _nvml(pynvml.nvmlDeviceGetTemperature, GPU, pynvml.NVML_TEMPERATURE_GPU)
                row["sm_clock_mhz"] = _nvml(pynvml.nvmlDeviceGetClockInfo, GPU, pynvml.NVML_CLOCK_SM)
            self.rows.append(row)
            tick += 1
            self._halt.wait(max(0.0, self.interval - (time.perf_counter() - t)))

    def stop(self):
        self._halt.set()
        self.join(timeout=2)


def aggregate_window(tele: pd.DataFrame, t0: float, t1: float) -> dict:
    w = tele[(tele.t >= t0) & (tele.t <= t1)]
    if w.empty:
        return {}
    dur = t1 - t0
    return {
        "gpu_util_mean_pct": w.gpu_util_pct.mean(), "gpu_util_peak_pct": w.gpu_util_pct.max(),
        "vram_peak_mb": w.vram_mb.max(), "power_mean_w": w.power_w.mean(),
        "temp_max_c": w.temp_c.max(), "sm_clock_mean_mhz": w.sm_clock_mhz.mean(),
        "cpu_mean_pct": w.cpu_pct.mean(), "ollama_rss_peak_mb": w.ollama_rss_mb.max(),
        "energy_j": w.power_w.mean() * dur,
    }


# ---------------------------------------------------------------------------
# 3. Streaming inference with latency decomposition
# ---------------------------------------------------------------------------
def stream_chat(session, args, prompt, seed):
    payload = {
        "model": args.model, "stream": True, "keep_alive": "30m",
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
        "options": {"temperature": args.temperature, "num_predict": args.num_predict, "seed": seed},
    }
    t0 = time.perf_counter()
    stamps, pieces, final = [], [], {}
    with session.post(f"{args.url}/api/chat", json=payload, stream=True, timeout=(5, 120)) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            now = time.perf_counter()
            chunk = json.loads(line)
            piece = chunk.get("message", {}).get("content", "")
            if piece:
                stamps.append(now)
                pieces.append(piece)
            if chunk.get("done"):
                final = chunk
                break
    t_end = time.perf_counter()
    if not stamps:
        raise RuntimeError("no tokens received")

    itls = np.diff(stamps)
    n_stream = len(stamps)
    n_tok = final.get("eval_count") or n_stream
    decode_s = stamps[-1] - stamps[0]
    eval_dur = final.get("eval_duration", 0) / 1e9
    return {
        "t0": t0, "t_end": t_end, "text": "".join(pieces).strip(),
        "ttft_ms": (stamps[0] - t0) * 1e3, "e2e_ms": (t_end - t0) * 1e3,
        "tpot_ms": (decode_s / (n_stream - 1) * 1e3) if n_stream > 1 else np.nan,
        "itl_mean_ms": itls.mean() * 1e3 if len(itls) else np.nan,
        "itl_jitter_ms": itls.std(ddof=1) * 1e3 if len(itls) > 1 else np.nan,
        "tps_client": (n_stream - 1) / decode_s if decode_s > 0 else np.nan,
        "tps_server": n_tok / eval_dur if eval_dur > 0 else np.nan,
        "n_tokens": n_tok,
        "prompt_tokens": final.get("prompt_eval_count", np.nan),
        "prompt_eval_ms": final.get("prompt_eval_duration", 0) / 1e6,
        "load_ms": final.get("load_duration", 0) / 1e6,
        "truncated": final.get("done_reason") == "length",
        "itls_ms": (itls * 1e3).tolist(),
    }


# ---------------------------------------------------------------------------
# 4. Deterministic persona-quality metrics
# ---------------------------------------------------------------------------
def text_metrics(text: str) -> dict:
    low = text.lower()
    sentences = [s.strip() for s in re.split(r"[.!?\u2026]+", text) if s.strip()]
    lens = np.array([len(s.split()) for s in sentences], dtype=float)
    return {
        "has_bullets": bool(BULLET_RE.search(text)),
        "trope_hits": sum(len(re.findall(p, low)) for p in TROPE_PATTERNS),
        "n_words": len(text.split()),
        "n_sentences": len(sentences),
        "sent_len_cv": float(lens.std() / lens.mean()) if len(lens) > 1 and lens.mean() > 0 else np.nan,
    }


def distinct_n(texts, n):
    grams, total = set(), 0
    for t in texts:
        toks = re.findall(r"[a-z']+", t.lower())
        for i in range(len(toks) - n + 1):
            grams.add(tuple(toks[i:i + n]))
            total += 1
    return len(grams) / total if total else np.nan


# ---------------------------------------------------------------------------
# 5. LLM-as-a-judge (rubric-anchored, JSON-first so output is never truncated)
# ---------------------------------------------------------------------------
JUDGE_SYSTEM = "You are a strict, well-calibrated linguistic evaluator. Reply with JSON only."
JUDGE_TEMPLATE = """Evaluate an AI companion's reply. The companion's persona instructions were:
\"\"\"{persona}\"\"\"

User message:
\"\"\"{user}\"\"\"

Companion reply:
\"\"\"{reply}\"\"\"

Score each criterion with an integer 1-10 using these anchors:
- naturalness: 1-3 stiff/robotic; 4-6 mixed; 7-8 casual, human-like; 9-10 indistinguishable from a real friend talking.
- relevance: 1-3 ignores the message or hallucinates specifics; 4-6 partially on-topic; 7-8 on-topic and sensible; 9-10 directly and precisely addresses it.
- cadence: 1-3 bullets, headings, or formal openers/closers; 4-6 some assistant tropes; 7-8 mostly fragments, pauses, varied length; 9-10 fully natural rhythm with zero assistant tropes.

Think briefly first, then give scores. Output ONLY this JSON:
{{"reasoning": "<max 40 words>", "naturalness": <int>, "relevance": <int>, "cadence": <int>}}"""


def judge_once(session, args, user, reply, seed):
    payload = {
        "model": args.judge_model, "stream": False, "format": "json", "keep_alive": "30m",
        "messages": [{"role": "system", "content": JUDGE_SYSTEM},
                     {"role": "user", "content": JUDGE_TEMPLATE.format(persona=SYSTEM_PROMPT, user=user, reply=reply)}],
        "options": {"temperature": args.judge_temperature, "num_predict": 300, "seed": seed},
    }
    try:
        r = session.post(f"{args.url}/api/chat", json=payload, timeout=120)
        r.raise_for_status()
        raw = r.json()["message"]["content"]
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            d = {k: re.search(rf'"{k}"\s*:\s*(\d+(?:\.\d+)?)', raw) for k in ("naturalness", "relevance", "cadence")}
            d = {k: float(m.group(1)) for k, m in d.items() if m}
        out = {k: float(d[k]) for k in ("naturalness", "relevance", "cadence")}
        if all(1 <= v <= 10 for v in out.values()):
            return out
    except Exception:  # noqa: BLE001
        pass
    return None  # caller counts failures; NEVER substitute a fake default score


# ---------------------------------------------------------------------------
# 6. Statistics
# ---------------------------------------------------------------------------
def boot_ci(a, stat=np.median, B=BOOTSTRAP_RESAMPLES, seed=0):
    if len(a) < 2:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    vals = np.array([stat(rng.choice(a, len(a), replace=True)) for _ in range(B)])
    return tuple(np.percentile(vals, [2.5, 97.5]))


def summarize(name, x, unit="", scale=1.0):
    a = np.asarray(x, dtype=float) * scale
    a = a[np.isfinite(a)]
    n = len(a)
    if n == 0:
        return {"metric": name, "unit": unit, "n": 0}
    q1, med, q3, p5, p95, p99 = np.percentile(a, [25, 50, 75, 5, 95, 99])
    sd = a.std(ddof=1) if n > 1 else 0.0
    ci = stats.t.ppf(0.975, n - 1) * sd / np.sqrt(n) if n > 1 else 0.0
    lo, hi = boot_ci(a)
    return {"metric": name, "unit": unit, "n": n, "mean": a.mean(), "std": sd, "ci95_half": ci,
            "median": med, "median_ci_lo": lo, "median_ci_hi": hi, "iqr": q3 - q1,
            "p5": p5, "p95": p95, "p99": p99, "min": a.min(), "max": a.max()}


def wilson(k, n, z=1.96):
    if n == 0:
        return np.nan, np.nan, np.nan
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, c - h, c + h


# ---------------------------------------------------------------------------
# 7. Plotting (IEEE-style: serif, embedded TrueType fonts, colour-blind-safe)
# ---------------------------------------------------------------------------
BASE_RC = {"font.family": "serif", "font.serif": ["Times New Roman", "Times", "DejaVu Serif"],
           "mathtext.fontset": "stix", "pdf.fonttype": 42, "ps.fonttype": 42,
           "axes.grid": True, "grid.alpha": 0.3, "grid.linestyle": ":", "axes.axisbelow": True,
           "axes.spines.top": False, "axes.spines.right": False, "savefig.bbox": "tight"}
PAPER_RC = {**BASE_RC, "font.size": 8, "axes.labelsize": 8, "axes.titlesize": 8.5,
            "legend.fontsize": 6.5, "xtick.labelsize": 7, "ytick.labelsize": 7}
DASH_RC = {**BASE_RC, "font.size": 10, "axes.labelsize": 10, "axes.titlesize": 11,
           "legend.fontsize": 8.5, "xtick.labelsize": 9, "ytick.labelsize": 9}


def panel_latency_cdf(ax, C):
    for arr, lab, col, ls in ((C.ttft, "TTFT", BLUE, "-"), (C.e2e, "End-to-end", VERM, "--")):
        x = np.sort(arr)
        y = np.arange(1, len(x) + 1) / len(x)
        p50, p95 = np.percentile(x, [50, 95])
        ax.step(x, y, where="post", color=col, ls=ls, lw=1.6, label=f"{lab} (P50 {p50:.0f} / P95 {p95:.0f} ms)")
        ax.axvline(p95, color=col, ls=":", lw=0.9)
    ax.axhline(0.95, color=GREY, lw=0.6)
    ax.set(xlabel="Latency (ms)", ylabel="Cumulative probability", title="(a) Latency CDF", ylim=(0, 1.02))
    ax.legend(loc="lower right")


def panel_itl(ax, C):
    x = C.itl[C.itl > 0]
    if len(x) == 0:
        ax.text(0.5, 0.5, "no ITL data", ha="center", transform=ax.transAxes)
        return
    bins = np.logspace(np.log10(x.min()), np.log10(x.max()), 60)
    ax.hist(x, bins=bins, color=SKY, edgecolor="white", lw=0.3)
    for q, col in ((50, GREEN), (95, ORANGE), (99, VERM)):
        v = np.percentile(x, q)
        ax.axvline(v, color=col, lw=1.3, label=f"P{q} = {v:.1f} ms")
    ax.set_xscale("log")
    ax.set(xlabel="Inter-token latency (ms, log)", ylabel="Token count",
           title=f"(b) ITL distribution ({len(x):,} tokens)")
    ax.legend()


def panel_throughput(ax, C):
    y = C.req.tps_server.to_numpy()
    x = np.arange(1, len(y) + 1)
    ax.scatter(x, y, s=9, color=GREEN, alpha=0.5, label="Per request")
    roll = pd.Series(y).rolling(max(5, len(y) // 10), center=True, min_periods=1).median()
    ax.plot(x, roll, color="black", lw=1.4, label="Rolling median")
    med = np.nanmedian(y)
    ax.axhline(med, color=VERM, ls="--", lw=1.1, label=f"Median {med:.1f} tok/s")
    ax.set(xlabel="Request index (execution order)", ylabel="Decode throughput (tok/s)",
           title="(c) Throughput stability")
    ax.legend()


def panel_prompt_ttft(ax, C):
    groups = [g.ttft_ms.to_numpy() for _, g in C.req.groupby("prompt_id")]
    labels = [f"P{i + 1}" for i in range(len(groups))]
    bp = ax.boxplot(groups, patch_artist=True, widths=0.6, medianprops={"color": "black"},
                    flierprops={"markersize": 2.5, "alpha": 0.5})
    for patch in bp["boxes"]:
        patch.set(facecolor=SKY, alpha=0.8)
    ax.set_xticklabels(labels)
    ax.set(xlabel="Test prompt (see summary.md)", ylabel="TTFT (ms)", title="(d) TTFT by prompt")


def panel_judge(ax, C):
    if C.judge.empty:
        ax.text(0.5, 0.5, "judge skipped / no valid scores", ha="center", transform=ax.transAxes)
        ax.set_title("(e) Persona quality")
        return
    cols = ["naturalness", "relevance", "cadence", "overall"]
    colors = [BLUE, GREEN, ORANGE, PURPLE]
    rng = np.random.default_rng(1)
    for i, (c, col) in enumerate(zip(cols, colors)):
        v = C.judge[c].dropna().to_numpy()
        m = v.mean()
        ci = stats.t.ppf(0.975, len(v) - 1) * v.std(ddof=1) / np.sqrt(len(v)) if len(v) > 1 else 0
        ax.bar(i, m, color=col, alpha=0.85, width=0.6, yerr=ci, capsize=4, error_kw={"lw": 1.2})
        ax.scatter(i + rng.uniform(-0.18, 0.18, len(v)), v, s=5, color="black", alpha=0.2, zorder=3)
        ax.text(i, min(m + ci + 0.3, 9.6), f"{m:.2f}", ha="center", fontweight="bold")
    ax.set_xticks(range(4))
    ax.set_xticklabels([c.capitalize() for c in cols])
    ax.set(ylim=(0, 10.5), ylabel="Score (1-10), mean \u00b1 95% CI",
           title=f"(e) LLM-judge (n={len(C.judge)}, parse ok {C.judge_ok_rate:.0%})")


def panel_hw(ax, C):
    t = C.tele
    if t.empty:
        return
    t = t[t.t >= C.t_measure_start].copy()
    t["rel"] = t.t - C.t_measure_start
    t = t[t.rel <= 60]
    for _, r in C.req.iterrows():
        if r.t0 - C.t_measure_start <= 60:
            ax.axvspan(r.t0 - C.t_measure_start, r.t_end - C.t_measure_start, color=GREY, alpha=0.12, lw=0)
    if t.gpu_util_pct.notna().any():
        l1, = ax.plot(t.rel, t.gpu_util_pct, color=VERM, lw=1.2, label="GPU util (%)")
        ax.set_ylabel("GPU utilisation (%)")
        ax.set_ylim(0, 105)
        ax2 = ax.twinx()
        ax2.spines["right"].set_visible(True)
        ax2.grid(False)
        l2, = ax2.plot(t.rel, t.power_w, color=BLUE, lw=1.2, label="Board power (W)")
        ax2.set_ylabel("Power (W)")
        ax.legend(handles=[l1, l2], loc="upper right")
    else:
        ax.plot(t.rel, t.cpu_pct, color=BLUE, label="Host CPU (%)")
        ax.set_ylabel("CPU utilisation (%)")
        ax.legend()
    ax.set(xlabel="Time since start of measurement (s); shaded = active request",
           title=f"(f) Hardware telemetry ({C.gpu_name})")


PANELS = [("latency_cdf", panel_latency_cdf), ("itl_distribution", panel_itl),
          ("throughput_stability", panel_throughput), ("ttft_by_prompt", panel_prompt_ttft),
          ("judge_scores", panel_judge), ("hardware_timeline", panel_hw)]


def make_figures(out: Path, C, footer):
    figs = out / "figures"
    figs.mkdir(exist_ok=True)
    with plt.rc_context(DASH_RC):
        fig, axs = plt.subplots(2, 3, figsize=(16, 8.6))
        for ax, (_, fn) in zip(axs.ravel(), PANELS):
            fn(ax, C)
        fig.suptitle(f"Lumi Local-LLM Benchmark \u2014 {C.model} on {C.gpu_name} (N={len(C.req)})",
                     fontsize=15, fontweight="bold")
        fig.text(0.5, 0.005, footer, ha="center", fontsize=8, color=GREY)
        fig.tight_layout(rect=(0, 0.02, 1, 0.96))
        fig.savefig(out / "dashboard.png", dpi=300)
        fig.savefig(out / "dashboard.pdf")
        plt.close(fig)
    for name, fn in PANELS:
        with plt.rc_context(PAPER_RC):
            fig, ax = plt.subplots(figsize=(3.5, 2.6))  # IEEE single-column width
            fn(ax, C)
            fig.tight_layout()
            fig.savefig(figs / f"{name}.pdf")
            fig.savefig(figs / f"{name}.png", dpi=300)
            plt.close(fig)


# ---------------------------------------------------------------------------
# 8. Reporting
# ---------------------------------------------------------------------------
def fmt(v, d=2):
    return "" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"{v:.{d}f}"


def write_reports(out: Path, rows, rates, meta):
    pd.DataFrame(rows).to_csv(out / "summary.csv", index=False)

    md = ["# Lumi Benchmark Summary", "",
          f"Model `{meta['model']}` | GPU {meta['gpu_name']} | Ollama {meta.get('ollama_version', '?')} | "
          f"N={meta['n_success']} ok / {meta['n_failed']} failed | seed {meta['seed']}", "",
          "| Metric | Unit | N | Mean \u00b1 95% CI | Median [95% CI] | IQR | P95 | P99 |",
          "|---|---|---|---|---|---|---|---|"]
    tex = [r"\begin{tabular}{lrcccc}", r"\toprule",
           r"Metric & $N$ & Mean $\pm$ 95\% CI & Median [95\% CI] & P95 & P99 \\", r"\midrule"]
    for r in rows:
        if r["n"] == 0:
            continue
        md.append(f"| {r['metric']} | {r['unit']} | {r['n']} | {fmt(r['mean'])} \u00b1 {fmt(r['ci95_half'])} | "
                  f"{fmt(r['median'])} [{fmt(r['median_ci_lo'])}, {fmt(r['median_ci_hi'])}] | "
                  f"{fmt(r['iqr'])} | {fmt(r['p95'])} | {fmt(r['p99'])} |")
        name = f"{r['metric']} ({r['unit']})".replace("_", r"\_").replace("%", r"\%") if r["unit"] else r["metric"]
        tex.append(f"{name} & {r['n']} & ${fmt(r['mean'])}\\pm{fmt(r['ci95_half'])}$ & "
                   f"{fmt(r['median'])} [{fmt(r['median_ci_lo'])}, {fmt(r['median_ci_hi'])}] & "
                   f"{fmt(r['p95'])} & {fmt(r['p99'])} \\\\")
    tex += [r"\bottomrule", r"\end{tabular}"]

    md += ["", "## Rates (Wilson 95% CI)", "", "| Metric | k/N | Rate | 95% CI |", "|---|---|---|---|"]
    for name, k, n in rates:
        p, lo, hi = wilson(k, n)
        md.append(f"| {name} | {k}/{n} | {p:.1%} | [{lo:.1%}, {hi:.1%}] |")

    md += ["", "## Prompts", ""] + [f"- **P{i + 1}**: {p}" for i, p in enumerate(TEST_PROMPTS)]
    if meta.get("judge_is_generator"):
        md += ["", "> **Note:** the judge model is the same as the generator model (self-preference bias is possible). "
               "Re-run with `--judge-model <different model>` for stronger evidence."]
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    (out / "summary.tex").write_text("\n".join(tex), encoding="utf-8")
    return "\n".join(md)


# ---------------------------------------------------------------------------
# 9. Main
# ---------------------------------------------------------------------------
def collect_metadata(args, session):
    meta = {"timestamp": datetime.now().isoformat(timespec="seconds"), "model": args.model,
            "judge_model": args.judge_model, "judge_is_generator": args.judge_model == args.model,
            "seed": args.seed, "temperature": args.temperature, "num_predict": args.num_predict,
            "reps_per_prompt": args.reps, "judge_samples": args.judge_samples,
            "platform": platform.platform(), "python": sys.version.split()[0],
            "cpu": platform.processor(), "cpu_cores_logical": psutil.cpu_count(),
            "ram_total_gb": round(psutil.virtual_memory().total / 1024 ** 3, 1),
            "numpy": np.__version__, "pandas": pd.__version__, "gpu_name": "N/A"}
    if HAS_NVML:
        name = pynvml.nvmlDeviceGetName(GPU)
        meta["gpu_name"] = name.decode() if isinstance(name, bytes) else name
        meta["gpu_vram_total_mb"] = round(pynvml.nvmlDeviceGetMemoryInfo(GPU).total / 1024 ** 2)
        drv = pynvml.nvmlSystemGetDriverVersion()
        meta["nvidia_driver"] = drv.decode() if isinstance(drv, bytes) else drv
    try:
        meta["ollama_version"] = session.get(f"{args.url}/api/version", timeout=5).json().get("version")
        show = session.post(f"{args.url}/api/show", json={"model": args.model, "name": args.model}, timeout=10).json()
        meta["model_details"] = show.get("details", {})
    except Exception:  # noqa: BLE001
        pass
    return meta


def main():
    ap = argparse.ArgumentParser(description="Lumi LLM benchmark suite")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--judge-model", default=None, help="defaults to --model (see bias note in summary)")
    ap.add_argument("--reps", type=int, default=20, help="repetitions per prompt (N = reps x prompts)")
    ap.add_argument("--num-predict", type=int, default=100)
    ap.add_argument("--temperature", type=float, default=0.8)
    ap.add_argument("--judge-samples", type=int, default=3)
    ap.add_argument("--judge-temperature", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--skip-judge", action="store_true")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()
    args.judge_model = args.judge_model or args.model

    random.seed(args.seed)
    np.random.seed(args.seed)
    session = requests.Session()
    try:
        session.get(f"{args.url}/api/version", timeout=5).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"[ERROR] Cannot reach Ollama at {args.url}: {exc}")

    out = Path(args.out) / f"lumi_bench_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    meta = collect_metadata(args, session)
    print(f"=== Lumi Benchmark | {meta['model']} | {meta['gpu_name']} | Ollama {meta.get('ollama_version')} ===")

    # ---- warm-up (unrecorded; loads weights, primes KV/cuda kernels) ----
    print(f"[Warm-up] {WARMUP_RUNS} unrecorded requests...")
    for i in range(WARMUP_RUNS):
        stream_chat(session, args, TEST_PROMPTS[i % len(TEST_PROMPTS)], seed=10_000 + i)

    # ---- measured phase ----
    sampler = TelemetrySampler()
    sampler.start()
    print(f"[Idle] Recording {IDLE_BASELINE_SEC:.0f}s idle baseline...")
    time.sleep(IDLE_BASELINE_SEC)
    t_measure_start = time.perf_counter()

    schedule = [(i, p) for i, p in enumerate(TEST_PROMPTS) for _ in range(args.reps)]
    random.shuffle(schedule)  # avoid order / thermal confounds per prompt

    records, itl_rows, failed = [], [], 0
    for run_idx, (pid, prompt) in enumerate(schedule):
        try:
            r = stream_chat(session, args, prompt, seed=args.seed + run_idx)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [{run_idx + 1}/{len(schedule)}] FAILED: {exc}")
            continue
        r.update({"run_idx": run_idx, "prompt_id": pid, "prompt": prompt})
        records.append(r)
        itl_rows += [(run_idx, pid, v) for v in r["itls_ms"]]
        print(f"  [{run_idx + 1}/{len(schedule)}] P{pid + 1} TTFT {r['ttft_ms']:.0f} ms | "
              f"{r['tps_server']:.1f} tok/s | E2E {r['e2e_ms']:.0f} ms")
        time.sleep(0.5)  # conversational gap
    t_measure_end = time.perf_counter()
    sampler.stop()
    if not records:
        sys.exit("[ERROR] no successful requests")

    tele = pd.DataFrame(sampler.rows)
    idle = tele[tele.t < t_measure_start]
    idle_power = idle.power_w.mean()

    req = pd.DataFrame(records)
    hw = pd.DataFrame([aggregate_window(tele, r.t0, r.t_end) for r in req.itertuples()])
    req = pd.concat([req.reset_index(drop=True), hw], axis=1)
    req["energy_per_token_j"] = req.energy_j / req.n_tokens
    tm = pd.DataFrame([text_metrics(t) for t in req.text])
    req = pd.concat([req, tm], axis=1)

    # ---- judge phase (AFTER measurement so it can't perturb latency/thermals) ----
    judge_rows, judge_calls, judge_ok = [], 0, 0
    if not args.skip_judge:
        print(f"[Judge] {args.judge_model}, {args.judge_samples} samples x {len(req)} responses...")
        if args.judge_model == args.model:
            print("  [Note] judge == generator: self-preference bias possible; consider --judge-model.")
        for r in req.itertuples():
            scores = []
            for s in range(args.judge_samples):
                judge_calls += 1
                res = judge_once(session, args, r.prompt, r.text, seed=args.seed * 1000 + r.run_idx * 10 + s)
                if res:
                    judge_ok += 1
                    scores.append(res)
            if scores:
                row = {k: float(np.mean([s[k] for s in scores])) for k in ("naturalness", "relevance", "cadence")}
                row["overall"] = float(np.mean(list(row.values())))
                row.update({"run_idx": r.run_idx, "prompt_id": r.prompt_id, "n_valid_samples": len(scores)})
                judge_rows.append(row)
    judge = pd.DataFrame(judge_rows)
    judge_ok_rate = judge_ok / judge_calls if judge_calls else float("nan")

    # ---- save raw data ----
    meta.update({"n_success": len(req), "n_failed": failed, "idle_power_w": float(idle_power),
                 "wall_clock_sec": round(t_measure_end - t_measure_start, 1)})
    (out / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    req.drop(columns=["itls_ms", "text", "t0", "t_end"]).to_csv(out / "requests.csv", index=False)
    pd.DataFrame(itl_rows, columns=["run_idx", "prompt_id", "itl_ms"]).to_csv(out / "itl_tokens.csv", index=False)
    tele.assign(t_rel=tele.t - t_measure_start).to_csv(out / "telemetry.csv", index=False)
    judge.to_csv(out / "judge.csv", index=False)
    with open(out / "responses.jsonl", "w", encoding="utf-8") as f:
        for r in req.itertuples():
            f.write(json.dumps({"run_idx": r.run_idx, "prompt": r.prompt, "response": r.text}) + "\n")

    # ---- statistics ----
    itl_all = np.array([v for _, _, v in itl_rows])
    rows = [
        summarize("TTFT", req.ttft_ms, "ms"), summarize("E2E latency", req.e2e_ms, "ms"),
        summarize("TPOT", req.tpot_ms, "ms"), summarize("ITL (pooled tokens)", itl_all, "ms"),
        summarize("ITL jitter (per-request std)", req.itl_jitter_ms, "ms"),
        summarize("Prompt eval time", req.prompt_eval_ms, "ms"),
        summarize("Throughput (client)", req.tps_client, "tok/s"),
        summarize("Throughput (server)", req.tps_server, "tok/s"),
        summarize("GPU utilisation", req.gpu_util_mean_pct, "%"),
        summarize("VRAM peak", req.vram_peak_mb, "MB"),
        summarize("GPU power", req.power_mean_w, "W"),
        summarize("GPU temperature (max)", req.temp_max_c, "C"),
        summarize("Energy per token", req.energy_per_token_j, "J/tok"),
        summarize("Ollama RSS peak", req.ollama_rss_peak_mb, "MB"),
        summarize("Response length", req.n_words, "words"),
        summarize("Sentence-length CV", req.sent_len_cv, ""),
    ]
    for c in ("naturalness", "relevance", "cadence", "overall"):
        if not judge.empty:
            rows.append(summarize(f"Judge {c}", judge[c], "1-10"))
    distinct2 = np.nanmean([distinct_n(g.text.tolist(), 2) for _, g in req.groupby("prompt_id")])
    distinct1 = np.nanmean([distinct_n(g.text.tolist(), 1) for _, g in req.groupby("prompt_id")])
    n = len(req)
    rates = [("Bullet-point violations", int(req.has_bullets.sum()), n),
             ("Assistant-trope violations", int((req.trope_hits > 0).sum()), n),
             ("Persona-constraint compliance (no bullets, no tropes)",
              int(((~req.has_bullets) & (req.trope_hits == 0)).sum()), n),
             ("Truncated at num_predict", int(req.truncated.sum()), n),
             ("Request success", n, n + failed)]
    if judge_calls:
        rates.append(("Judge JSON parse success", judge_ok, judge_calls))
    meta["distinct_1_within_prompt"] = float(distinct1)
    meta["distinct_2_within_prompt"] = float(distinct2)
    (out / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    md = write_reports(out, rows, rates, meta)

    # ---- figures ----
    C = SimpleNamespace(
        req=req, tele=tele, judge=judge, judge_ok_rate=judge_ok_rate, model=args.model,
        gpu_name=meta["gpu_name"], t_measure_start=t_measure_start,
        ttft=req.ttft_ms.to_numpy(), e2e=req.e2e_ms.to_numpy(), itl=itl_all)
    footer = (f"Ollama {meta.get('ollama_version', '?')} | temp {args.temperature} | num_predict {args.num_predict} | "
              f"seed {args.seed} | idle power {idle_power:.1f} W | Distinct-2 {distinct2:.2f} | "
              f"judge: {args.judge_model} x{args.judge_samples}")
    make_figures(out, C, footer)

    print("\n" + md)
    print(f"\nDistinct-1 / Distinct-2 (within-prompt): {distinct1:.3f} / {distinct2:.3f}")
    print(f"Idle GPU power baseline: {idle_power:.1f} W")
    print(f"\nAll outputs saved to: {out.resolve()}")


if __name__ == "__main__":
    main()