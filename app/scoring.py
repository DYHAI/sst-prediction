"""评分与检验。

设计原则（对应文献里的做法，见 DESIGN.md）：

1. 所有基础指标都是气象/海洋预报检验的标准量：MAE、RMSE、Bias、
   命中率、以及相对技巧分（skill score）。
2. 综合分不是拍脑袋的加权，而是**先把每个指标归一化，再按公开权重融合**，
   并且每个 (海区, 时效) 分组各自归一，避免"好预测的海区"或"短期时效"
   把总分抬得太高。
3. 显著性用 Diebold–Mariano 检验（Newey–West 标准误），和论文一致。

参考标准（这件事必须说清楚，不然分数没意义）：
    "标准" = 同一 (海区, 时效) 分组里**表现最好的官方预报产品**的 MAE。
    官方产品包括 HYCOM、GFS、CFSv2，它们每天被同一套规则抓取、同样用 OISST 结算。
    所以基准不是我们拍的数字，而是"目前官方能给出的最好水平"。

综合分公式（总分 0–100）：

    score = 100 * clip( W_ACC * AccScore + W_BIAS * BiasScore + W_HIT * HitRate, 0, 1 )

其中，先按 (海区, 时效) 分组分别算优势，再对各组求平均：
    adv       = 1 - MAE / MAE_ref          （正数 = 比官方最好还准）
    AccScore  = clip(0.5 + adv, 0, 1)      （与官方最好打平 = 0.50）
    BiasScore = exp(-|Bias| / BIAS_TAU)    （BIAS_TAU 默认 0.5 °C）
    HitRate   = P(|误差| <= HIT_TOL)       （HIT_TOL 默认 0.5 °C）

权重默认 (W_ACC, W_BIAS, W_HIT) = (0.45, 0.20, 0.35)，全部可在
data/config.json 的 scoring 段里改，界面会显示当前权重。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

DEFAULT_WEIGHTS = {"acc": 0.45, "bias": 0.20, "hit": 0.35}
BIAS_TAU = 0.5      # °C，偏差衰减尺度
HIT_TOL = 0.5       # °C，命中判定的容差
MIN_N = 10          # 少于这么多次验证就不给综合分，只显示原始指标


@dataclass
class Verif:
    """一条已结算记录。"""

    entity: str
    region: str
    horizon: int
    target_date: str
    forecast: float
    truth: float

    @property
    def error(self) -> float:
        return self.forecast - self.truth


@dataclass
class Metrics:
    n: int = 0
    mae: float = float("nan")
    rmse: float = float("nan")
    bias: float = float("nan")
    hit: float = float("nan")
    acc: float = float("nan")
    advantage: float = float("nan")   # 相对官方最好产品的相对 MAE 优势
    score: float = float("nan")
    by_horizon: dict = field(default_factory=dict)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def basic_metrics(errors: list[float]) -> dict:
    """一组误差的基础指标。"""
    n = len(errors)
    if n == 0:
        return {"n": 0, "mae": None, "rmse": None, "bias": None, "hit": None}
    abs_err = [abs(e) for e in errors]
    return {
        "n": n,
        "mae": _mean(abs_err),
        "rmse": math.sqrt(_mean([e * e for e in errors])),
        "bias": _mean(errors),
        "hit": sum(1 for a in abs_err if a <= HIT_TOL) / n,
    }


def reference_mae(records: list[Verif], reference_entity: str = "__persistence__") -> dict:
    """按 (region, horizon) 计算参考预报的 MAE。"""
    groups: dict[tuple[str, int], list[float]] = {}
    for r in records:
        groups.setdefault((r.region, r.horizon), []).append(r)

    ref: dict[tuple[str, int], float] = {}
    for key, rs in groups.items():
        ref_errs = [abs(r.error) for r in rs if r.entity == reference_entity]
        if ref_errs:
            ref[key] = _mean(ref_errs)
        else:
            all_errs = sorted(abs(r.error) for r in rs)
            if not all_errs:
                continue
            mid = len(all_errs) // 2
            med = all_errs[mid] if len(all_errs) % 2 else 0.5 * (all_errs[mid - 1] + all_errs[mid])
            ref[key] = max(med, 1e-6)
    return ref


def reference_from_products(records: list[Verif], product_entities: set[str]) -> dict:
    """参考标准 = 每个分组里最好的官方产品的 MAE（取各组内最小值）。

    分组内没有任何官方产品数据时，退回该组所有实体的中位误差，
    这样榜单冷启动阶段也能算分。
    """
    groups: dict[tuple[str, int], list[Verif]] = {}
    for r in records:
        groups.setdefault((r.region, r.horizon), []).append(r)

    ref: dict[tuple[str, int], float] = {}
    for key, rs in groups.items():
        per_product: dict[str, list[float]] = {}
        for r in rs:
            if r.entity in product_entities:
                per_product.setdefault(r.entity, []).append(abs(r.error))
        if per_product:
            ref[key] = min(_mean(v) for v in per_product.values())
            continue
        all_errs = sorted(abs(r.error) for r in rs)
        if not all_errs:
            continue
        mid = len(all_errs) // 2
        med = all_errs[mid] if len(all_errs) % 2 else 0.5 * (all_errs[mid - 1] + all_errs[mid])
        ref[key] = max(med, 1e-6)
    return ref


def score_entity(
    records: list[Verif],
    ref: dict[tuple[str, int], float],
    weights: dict | None = None,
    bias_tau: float = BIAS_TAU,
    hit_tol: float = HIT_TOL,
) -> Metrics:
    """给定一个实体的全部已结算记录，算基础指标 + 综合分。"""
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)

    m = Metrics()
    if not records:
        return m

    errors = [r.error for r in records]
    base = basic_metrics(errors)
    m.n = base["n"]
    m.mae = base["mae"]
    m.rmse = base["rmse"]
    m.bias = base["bias"]
    m.hit = base["hit"]

    # 分 (region, horizon) 各自算优势，再平均
    groups: dict[tuple[str, int], list[Verif]] = {}
    for r in records:
        groups.setdefault((r.region, r.horizon), []).append(r)

    acc_terms: list[float] = []
    adv_terms: list[float] = []
    per_h: dict[int, dict] = {}
    for (region, horizon), rs in groups.items():
        mae_group = _mean([abs(x.error) for x in rs])
        target = ref.get((region, horizon))
        if not target or target <= 0:
            target = max(mae_group, 1e-6)
        adv = 1.0 - mae_group / target
        adv_terms.append(adv)
        acc_terms.append(max(0.0, min(1.0, 0.5 + adv)))

        hb = per_h.setdefault(horizon, {"n": 0, "_abs": [], "_err": [], "_hit": 0})
        for x in rs:
            hb["n"] += 1
            hb["_abs"].append(abs(x.error))
            hb["_err"].append(x.error)
            if abs(x.error) <= hit_tol:
                hb["_hit"] += 1

    m.acc = _mean(acc_terms)
    m.advantage = _mean(adv_terms)
    bias_score = math.exp(-abs(m.bias) / bias_tau)
    hit_rate = m.hit

    raw = w["acc"] * m.acc + w["bias"] * bias_score + w["hit"] * hit_rate
    m.score = 100.0 * max(0.0, min(1.0, raw)) if m.n >= MIN_N else float("nan")

    for horizon, hb in per_h.items():
        hb["mae"] = _mean(hb.pop("_abs"))
        errs = hb.pop("_err")
        hb["rmse"] = math.sqrt(_mean([e * e for e in errs]))
        hb["bias"] = _mean(errs)
        hb["hit"] = hb.pop("_hit") / hb["n"]
    m.by_horizon = dict(sorted(per_h.items()))
    return m


def skill_vs(errors: list[float], ref_errors: list[float]) -> float | None:
    """相对参考的 MSE 技巧分：1 - MSE/MSE_ref。"""
    if not errors or not ref_errors:
        return None
    mse = _mean([e * e for e in errors])
    mse_ref = _mean([e * e for e in ref_errors])
    if mse_ref <= 0:
        return None
    return 1.0 - mse / mse_ref


def dm_test(err_a: list[float], err_b: list[float], lag: int | None = None) -> dict:
    """Diebold–Mariano 检验（单侧：a 比 b 更准）。

    输入是同一批日期上两个预报的误差序列（长度相同、日期对齐）。
    返回 {stat, p_value, mean_diff}。用 Newey–West 修正自相关。
    """
    n = min(len(err_a), len(err_b))
    if n < 8:
        return {"stat": None, "p_value": None, "mean_diff": None, "n": n}
    d = [err_a[i] ** 2 - err_b[i] ** 2 for i in range(n)]
    mean_d = _mean(d)

    if lag is None:
        lag = max(1, int(round(n ** (1 / 3))))
    dev = [x - mean_d for x in d]
    gamma0 = sum(x * x for x in dev) / n
    var = gamma0
    for k in range(1, lag + 1):
        cov = sum(dev[i] * dev[i - k] for i in range(k, n)) / n
        var += 2.0 * (1.0 - k / (lag + 1.0)) * cov
    if var <= 0:
        return {"stat": 0.0, "p_value": 0.5, "mean_diff": mean_d, "n": n}
    stat = mean_d / math.sqrt(var / n)
    # 单侧 p 值：a 的误差比 b 小（mean_d < 0）时为小概率
    p = 0.5 * math.erfc(stat / math.sqrt(2))
    return {"stat": stat, "p_value": p, "mean_diff": mean_d, "n": n}
