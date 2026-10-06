"""探针：把这台机器上的神经网络引擎（ANE）真正用起来试试。

做法：把训练好的 U-Net 转成 Core ML，分别在「只用 CPU」和「CPU+GPU+ANE」
两种设置下跑同一个输入，比较延迟与输出一致性。

为什么 PyTorch 用不到 ANE：PyTorch 的 MPS 后端只调 Metal（GPU），
Apple 没开放让第三方框架直接调度 ANE 的接口。要用 ANE 必须走 Core ML
（由系统在 CPU/GPU/ANE 之间自动分配算子）。

用法：
    .venv/bin/python -m tools.ane_bench
"""

from __future__ import annotations

import os
import sys
import time

import numpy as np
import torch

from app.unet import UNet

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.path.join(BASE, "data", "models", "unet.pt")
H, W = 82, 56          # 南海盒子的网格


def main() -> int:
    if not os.path.exists(MODEL):
        print("还没有训练好的 U-Net，先跑 tools/train_unet.py", file=sys.stderr)
        return 1

    ckpt = torch.load(MODEL, map_location="cpu", weights_only=False)
    net = UNet(cin=ckpt.get("cin", 6), base=ckpt.get("base", 24),
               depth=ckpt.get("depth", 3))
    net.load_state_dict(ckpt["state"])
    net.eval()
    print(f"载入 U-Net：输入 {ckpt.get('cin', 6)}×{H}×{W}，"
          f"参数量 {sum(p.numel() for p in net.parameters())/1e3:.1f}K")

    x = torch.randn(1, ckpt.get("cin", 6), H, W)
    with torch.no_grad():
        ref = net(x).numpy()

    # --- 转 Core ML ---
    import coremltools as ct

    traced = torch.jit.trace(net, x)
    try:
        ml = ct.convert(
            traced,
            inputs=[ct.TensorType(name="x", shape=x.shape)],
            compute_units=ct.ComputeUnit.ALL,
            minimum_deployment_target=ct.target.macOS15,
            convert_to="mlprogram",
        )
    except Exception as e:  # noqa: BLE001
        print("Core ML 转换失败：", str(e)[:300])
        return 2

    def bench(units, label, n=30):
        try:
            m = ct.models.MLModel(ml.get_spec(), compute_units=units) \
                if hasattr(ct.models, "MLModel") else ml
        except Exception:
            m = ml
        out = m.predict({"x": x.numpy()})
        y = list(out.values())[0]
        for _ in range(3):                       # 预热
            m.predict({"x": x.numpy()})
        t0 = time.perf_counter()
        for _ in range(n):
            m.predict({"x": x.numpy()})
        dt = (time.perf_counter() - t0) / n * 1000
        diff = float(np.abs(y - ref).max())
        print(f"  {label:28s} {dt:7.2f} ms/次   与 PyTorch 最大差 {diff:.2e}")
        return dt

    print("\n推理延迟（同一份输入，30 次平均）：")
    t_cpu = bench(ct.ComputeUnit.CPU_ONLY, "只用 CPU")
    t_all = bench(ct.ComputeUnit.ALL, "CPU + GPU + 神经网络引擎")
    t_gpu = bench(ct.ComputeUnit.CPU_AND_GPU, "CPU + GPU（不含 ANE）")

    print("\n判读：")
    if t_all < min(t_cpu, t_gpu) * 0.95:
        print("  · ALL 明显快于 CPU+GPU，说明确实有第三个加速器（ANE）参与")
    elif abs(t_all - t_gpu) / max(t_gpu, 1e-9) < 0.1:
        print("  · ALL 与 CPU+GPU 基本一致，这个网络可能主要落在 GPU/CPU 上；")
        print("    ANE 对卷积友好，但对某些算子（插值、大核）会回落到 GPU。")
    else:
        print("  · 数据见上，差异不大，说明这个规模下 ANE 的优势有限。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
