"""神经网络引擎（ANE）实测：把南海 U-Net 转成 Core ML，分别用 CPU / GPU / ANE 跑。

为什么值得做这个对比：
  · llama.cpp 走 Metal，只能用 GPU，用不到 ANE（二进制里连 CoreML 都没链接）
  · ANE 只能通过 Core ML 访问，所以要用它必须先把模型转成 .mlpackage
  · Core ML 提供四种计算单元选择，可以干净地把三个引擎分开测：
        CPU_ONLY / CPU_AND_GPU / CPU_AND_NE（CPU+神经网络引擎）/ ALL

注意：这个脚本要在装了 coremltools **预编译 wheel** 的环境里跑。
Python 3.14 上 PyPI 只给源码包（编出来缺原生扩展），所以用 3.9 的独立环境：
    /usr/bin/python3 -m venv /tmp/cmvenv
    /tmp/cmvenv/bin/pip install coremltools numpy torch
    /tmp/cmvenv/bin/python tools/ane_vs_gpu.py
"""

from __future__ import annotations

import os
import statistics
import sys
import time

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

import coremltools as ct

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
H, W = 82, 56
CIN = 6


# ---------------------------------------------------------------- 模型

def conv_block(cin, cout):
    return nn.Sequential(
        nn.Conv2d(cin, cout, 3, padding=1, padding_mode="replicate"),
        nn.BatchNorm2d(cout), nn.SiLU(inplace=True),
        nn.Conv2d(cout, cout, 3, padding=1, padding_mode="replicate"),
        nn.BatchNorm2d(cout), nn.SiLU(inplace=True),
    )


class UNetFixed(nn.Module):
    """和线上 U-Net 同构，但上采样的尺寸写死。

    原因：线上版用 F.interpolate(x, size=skip.shape[-2:]) 动态读形状，
    coremltools 会把 shape 变成 int 张量、转不过去（报 "only 0-dimensional
    arrays can be converted to Python scalars"）。输入固定是 82×56，
    各层尺寸确定，直接写死即可。
    """

    def __init__(self, base=24, depth=3):
        super().__init__()
        self.enc = nn.ModuleList()
        self.pool = nn.MaxPool2d(2)
        c = CIN
        chans = []
        for i in range(depth):
            out = base * (2 ** i)
            self.enc.append(conv_block(c, out))
            chans.append(out)
            c = out
        self.mid = conv_block(c, base * (2 ** depth))
        self.dec = nn.ModuleList()
        cc = base * (2 ** depth)
        for i in reversed(range(depth)):
            self.dec.append(conv_block(cc + chans[i], chans[i]))
            cc = chans[i]
        self.head = nn.Conv2d(base, 1, 1)
        # 编码器各层输出尺寸（82×56 起），解码器的上采样目标正好是它的逆序
        enc_sizes, h, w = [(H, W)], H, W
        for _ in range(depth):
            h, w = h // 2, w // 2
            enc_sizes.append((h, w))
        # enc_sizes 的最后一项是瓶颈层，解码器上采样目标是它之前那些层的逆序
        self.sizes = list(reversed(enc_sizes[:-1]))

    def forward(self, x):
        skips = []
        for blk in self.enc:
            x = blk(x)
            skips.append(x)
            x = self.pool(x)
        x = self.mid(x)
        for i, blk in enumerate(self.dec):
            x = F.interpolate(x, size=self.sizes[i], mode="bilinear",
                              align_corners=False)
            x = blk(torch.cat([x, skips[-1 - i]], dim=1))
        out = self.head(x)
        return out


def load_trained() -> nn.Module:
    """尽量载入线上训练好的权重；尺寸对不上就退回随机初始化。"""
    m = UNetFixed()
    p = os.path.join(BASE, "data", "models", "unet.pt")
    if os.path.exists(p):
        try:
            ck = torch.load(p, map_location="cpu", weights_only=False)
            if ck.get("cin") == CIN:
                m.load_state_dict(ck["state"], strict=False)
                print("  已载入线上 U-Net 权重（strict=False）")
        except Exception as e:  # noqa: BLE001
            print("  权重载入失败，用随机初始化：", str(e)[:80])
    m.eval()
    return m


class TransformerBlock(nn.Module):
    """一个标准 transformer 解码层：自注意力 + 前馈。

    LLM 解码就是把这个块重复几十层。用它测 ANE，比拿整模型更好使——
    能直接看出"注意力/前馈这类算子"在哪块硬件上更快。

    这里用「整段序列一次算完」（prefill）的形态，也就是提示处理阶段；
    逐 token 解码（decode）是同一个块配 1 个 token。
    """

    def __init__(self, d_model=1024, n_head=8, d_ff=4096, seq=256):
        super().__init__()
        self.seq, self.d_model = seq, d_model
        self.ln1 = nn.LayerNorm(d_model)
        self.qkv = nn.Linear(d_model, 3 * d_model, bias=False)
        self.proj = nn.Linear(d_model, d_model, bias=False)
        self.ln2 = nn.LayerNorm(d_model)
        self.fc1 = nn.Linear(d_model, d_ff, bias=False)
        self.fc2 = nn.Linear(d_ff, d_model, bias=False)
        self.n_head = n_head

    def forward(self, x):                       # x: (B, seq, d_model)
        B, S, D = x.shape
        h = self.ln1(x)
        qkv = self.qkv(h).reshape(B, S, 3, self.n_head, D // self.n_head)
        q, k, v = qkv.unbind(dim=2)
        q, k, v = (t.transpose(1, 2) for t in (q, k, v))   # (B, h, S, d)
        att = torch.softmax(q @ k.transpose(-2, -1) / (D // self.n_head) ** 0.5, dim=-1)
        o = (att @ v).transpose(1, 2).reshape(B, S, D)
        x = x + self.proj(o)
        h = self.ln2(x)
        return x + self.fc2(F.silu(self.fc1(h)))


# ---------------------------------------------------------------- 主流程

def main() -> int:
    torch.manual_seed(0)
    for name, build, shape, tag in (
        ("南海 U-Net（卷积为主）", lambda: load_trained(), (1, CIN, H, W), "unet"),
        ("Transformer 层（注意力+前馈）",
         lambda: TransformerBlock(d_model=1024, n_head=8, d_ff=4096, seq=256).eval(),
         (1, 256, 1024), "tfm"),
    ):
        print(f"\n{'='*60}\n【{name}】")
        run_one(build(), shape, tag)
    return 0


def run_one(net: nn.Module, shape, tag: str) -> None:
    """同一个模型，四个计算单元各跑一遍。"""
    x = torch.randn(*shape)
    print("① 准备模型")
    with torch.no_grad():
        ref = net(x).numpy()
    print(f"  参数量 {sum(p.numel() for p in net.parameters())/1e6:.2f} M"
          f"，输出形状 {ref.shape}")

    print("② 转 Core ML")
    t0 = time.time()
    traced = torch.jit.trace(net, x)
    ml = ct.convert(
        traced,
        inputs=[ct.TensorType(name="x", shape=x.shape)],
        compute_units=ct.ComputeUnit.ALL,
        minimum_deployment_target=ct.target.macOS14,
        convert_to="mlprogram",
    )
    print(f"  转换完成 {time.time()-t0:.1f}s")

    print("③ 四个计算单元分别跑")
    # mlprogram 的权重要落在磁盘上，重新按不同计算单元加载时要给路径
    pkg = os.path.join("/tmp", f"{tag}_ane.mlpackage")
    ml.save(pkg)
    print(f"  已保存 {pkg}")
    xin = {"x": x.numpy()}
    results = {}
    for units, label in (
        (ct.ComputeUnit.CPU_ONLY, "只用 CPU"),
        (ct.ComputeUnit.CPU_AND_GPU, "CPU + GPU"),
        (ct.ComputeUnit.CPU_AND_NE, "CPU + 神经网络引擎"),
        (ct.ComputeUnit.ALL, "全部（CPU+GPU+ANE）"),
    ):
        m = ct.models.MLModel(pkg, compute_units=units)
        y = list(m.predict(xin).values())[0]
        for _ in range(5):                      # 预热
            m.predict(xin)
        ts = []
        for _ in range(30):
            t = time.perf_counter()
            m.predict(xin)
            ts.append(time.perf_counter() - t)
        med = statistics.median(ts) * 1000
        diff = float(np.abs(y - ref).max())
        results[label] = med
        print(f"  {label:20s} {med:7.2f} ms/次   与 PyTorch 最大差 {diff:.2e}")

    # 哪个引擎真的被用上了
    print("④ 计算单元分配（Core ML 自己报告的）")
    try:
        m = ct.models.MLModel(pkg, compute_units=ct.ComputeUnit.ALL)
        plan = m.get_compute_plan() if hasattr(m, "get_compute_plan") else None
        if plan is not None:
            used = {}
            for op in plan.model_structure.program.functions["main"].block.operations:
                for o in op.outputs:
                    for loc in plan.get_compute_device_usage_for_mlprogram_operation(op):
                        dev = str(loc.compute_device).split(".")[-1]
                        used[dev] = used.get(dev, 0) + 1
            print("  算子落在：", used)
        else:
            print("  （这个版本没有 get_compute_plan，跳过）")
    except Exception as e:  # noqa: BLE001
        print("  查询失败：", str(e)[:120])

    print("\n⑤ 结论")
    base = results.get("只用 CPU")
    for k, v in results.items():
        if base and k != "只用 CPU":
            print(f"  {k:20s} {v:7.2f} ms   相对纯 CPU 快 {base/v:.1f}×")
    return 0


if __name__ == "__main__":
    sys.exit(main())
