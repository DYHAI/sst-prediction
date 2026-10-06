"""本机大模型推理基准：测不同框架/模型的生成速度与内存占用。

用法：
    .venv/bin/python -m tools.llm_bench --model <路径> --kind mlx --n 256
    .venv/bin/python -m tools.llm_bench --model <路径.gguf> --kind llama --n 256
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

LLAMA_BIN = os.path.expanduser("~/src/llama.cpp/build/bin/llama-cli")


def bench_llama(model: str, prompt: str, n: int) -> dict:
    t0 = time.time()
    p = subprocess.run(
        [LLAMA_BIN, "-m", model, "-ngl", "99", "-c", "4096", "-n", str(n),
         "-t", "8", "-st", "--no-warmup", "-p", prompt],
        capture_output=True, text=True, timeout=3600)
    out = p.stdout + p.stderr
    gen = tps = None
    for line in out.splitlines():
        if "Generation:" in line and "t/s" in line:
            try:
                gen = float(line.split("Generation:")[1].split("t/s")[0].strip())
            except ValueError:
                pass
        if "Prompt:" in line and "t/s" in line:
            try:
                tps = float(line.split("Prompt:")[1].split("t/s")[0].strip())
            except ValueError:
                pass
    text = out.split("[End thinking]")[-1] if "[End thinking]" in out else out[-1500:]
    return {"framework": "llama.cpp", "gen_tps": gen, "prompt_tps": tps,
            "wall_s": round(time.time() - t0, 1), "snippet": text.strip()[:400]}


def bench_mlx(model: str, prompt: str, n: int) -> dict:
    """用 mlx_lm 的流式接口计时（Bonsai 那种自定义 loader 要走 quickstart）。"""
    import mlx.core as mx
    from mlx_lm import load, stream_generate

    t0 = time.time()
    m, tok = load(model)
    load_s = time.time() - t0
    ntok, t_first = 0, None
    for r in stream_generate(m, tok, prompt=prompt, max_tokens=n):
        if t_first is None:
            t_first = time.time()
        ntok += 1
    gen_s = time.time() - (t_first or time.time())
    mx.eval(mx.zeros(1))

    import re
    peak = None
    try:
        peak = mx.get_active_memory() / 1073741824
    except Exception:  # noqa: BLE001
        pass
    return {"framework": "MLX", "gen_tps": round(ntok / gen_s, 1) if gen_s > 0 else None,
            "load_s": round(load_s, 1), "gen_s": round(gen_s, 1),
            "peak_gb": None if peak is None else round(peak, 2),
            "wall_s": round(time.time() - t0, 1), "tokens": ntok}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--kind", choices=("llama", "mlx"), required=True)
    ap.add_argument("--prompt", default="用一句话说明什么是海洋热浪。")
    ap.add_argument("--n", type=int, default=256)
    a = ap.parse_args()

    r = bench_llama(a.model, a.prompt, a.n) if a.kind == "llama" \
        else bench_mlx(a.model, a.prompt, a.n)
    for k, v in r.items():
        print(f"  {k:12s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
