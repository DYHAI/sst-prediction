"""U-Net 多源订正模型：把模式预报场订正到 OISST 真值场。

为什么用 U-Net：海温的预报误差不是逐点独立的，而是有空间结构的
（近岸、陆架坡折、涡旋区误差大），卷积网络正好吃这个结构，这是逐格点回归做不到的。

输入 6 个通道（都对齐到 OISST 的 82×56 网格）：
   0 持续性距平场（as_of 当天的 OISST 距平）
   1 GFS 距平场（模式预报；缺数据时填 0）
   2 GFS 有无标志（0/1）
   3 季节 sin   4 季节 cos
   5 时效 / 5
输出 1 个通道：订正后的距平场。损失只在海格点上计算。

网格对齐：OISST 网格点从 3.125°N 起，GFS 从 3.0°N 起，差半个格点，
所以 GFS 要先做 2×2 平均挪半格，否则两个场会系统性错位。
"""

from __future__ import annotations

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


def conv_block(cin: int, cout: int) -> nn.Sequential:
    return nn.Sequential(
        nn.Conv2d(cin, cout, 3, padding=1, padding_mode="replicate"),
        nn.BatchNorm2d(cout),
        nn.SiLU(inplace=True),
        nn.Conv2d(cout, cout, 3, padding=1, padding_mode="replicate"),
        nn.BatchNorm2d(cout),
        nn.SiLU(inplace=True),
    )


class UNet(nn.Module):
    """残差式 U-Net：输出 = 持续性场 + 网络学到的修正量。

    直接从零预测距平会**过度阻尼**：网络为了压低方差，会把距平往 0 收缩，
    实测在那个窗口系统性偏冷 0.31°C，比持续性还差。
    改成"以持续性为底、只学修正"，最差也就是回到持续性。
    """

    def __init__(self, cin: int = 6, base: int = 24, depth: int = 3,
                 residual_channel: int | None = 0):
        super().__init__()
        self.residual_channel = residual_channel
        self.enc = nn.ModuleList()
        self.pool = nn.MaxPool2d(2)
        c = cin
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
            # 注意：瓶颈之后的通道数是逐层变的，不能一直用瓶颈的通道数
            self.dec.append(conv_block(cc + chans[i], chans[i]))
            cc = chans[i]
        self.head = nn.Conv2d(base, 1, 1)

    def forward(self, x):
        skips = []
        for blk in self.enc:
            x = blk(x)
            skips.append(x)
            x = self.pool(x)
        x = self.mid(x)
        for i, blk in enumerate(self.dec):
            skip = skips[-1 - i]
            x = F.interpolate(x, size=skip.shape[-2:], mode="bilinear",
                              align_corners=False)
            x = blk(torch.cat([x, skip], dim=1))
        out = self.head(x)
        if self.residual_channel is not None:
            out = out + x[:, self.residual_channel:self.residual_channel + 1]
        return out


# ---------------------------------------------------------------- 网格工具

def regrid_half_shift(field: np.ndarray) -> np.ndarray:
    """把 GFS（3.0°起）挪半格对齐到 OISST（3.125°起）。

    两套网格都从 105/3 度起、步长 0.25，只差半个格点，
    所以用 2×2 平均即可，不需要插值库。
    """
    g = np.pad(field, ((0, 1), (0, 1)), mode="edge")
    return 0.25 * (g[:-1, :-1] + g[:-1, 1:] + g[1:, :-1] + g[1:, 1:])


def load_gfs_grid(path: str):
    """读回补好的 GFS 南海盒子场：返回 (dates, array[ntime, nlead, nlat, nlon])。"""
    import json
    import os

    with open(os.path.join(path, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    nlat, nlon, nlead = meta["nlat"], meta["nlon"], len(meta["leads"])
    raw = np.fromfile(os.path.join(path, "data.bin"), dtype="<f4")
    ntime = len(meta["dates"])
    arr = raw.reshape(ntime, nlead, nlat, nlon)
    return meta["dates"], arr
