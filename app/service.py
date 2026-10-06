"""业务层：把数据库里的提交/产品/真值拼成榜单。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from . import db, products as P, scoring
from . import mhw
from . import regions as R

PRODUCT_ENTITIES = {f"product:{code}" for code in P.PRODUCTS}
REFERENCE_ENTITIES = {f"product:{code}" for code in P.REFERENCE_PRODUCTS}
BASELINE_ENTITIES = {"baseline:persistence"}
BASELINE_META = {
    "baseline:persistence": {
        "name": "持续性基线",
        "kind": "baseline",
        "org": "本站内置对照",
        "note": "最简单的参照：把目标日往前 h 天的 OISST 实测值直接当成预报，不做任何模型。",
    },
}


def _since(window_days: int) -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=window_days)).isoformat()


def load_entries(conn, window_days: int | None = 60) -> list[scoring.Verif]:
    """把所有已结算（有真值）的预报拼成 Verif 列表。"""
    out: list[scoring.Verif] = []
    where, args = "", []
    if window_days:
        where = " WHERE s.target_date >= ?"
        args = [_since(window_days)]

    sql = f"""
        SELECT 'player:' || p.id AS entity, s.region, s.horizon, s.target_date,
               s.value AS forecast, t.sst AS truth
        FROM submissions s
        JOIN truth t ON t.region = s.region AND t.date = s.target_date
        JOIN players p ON p.id = s.player_id
        {where}
    """
    for row in conn.execute(sql, args):
        out.append(scoring.Verif(row["entity"], row["region"], row["horizon"],
                                 row["target_date"], row["forecast"], row["truth"]))

    sql = f"""
        SELECT 'product:' || pr.product AS entity, pr.region, pr.horizon,
               pr.target_date, pr.value AS forecast, t.sst AS truth
        FROM products pr
        JOIN truth t ON t.region = pr.region AND t.date = pr.target_date
        {where.replace('s.target_date', 'pr.target_date')}
    """
    for row in conn.execute(sql, args):
        out.append(scoring.Verif(row["entity"], row["region"], row["horizon"],
                                 row["target_date"], row["forecast"], row["truth"]))

    # 内置对照：持续性基线（用 h 天前的实测值当预报），不占用产品名额
    for h in R.HORIZONS:
        sql = """
            SELECT t1.region, t2.date AS target_date, t1.sst AS forecast, t2.sst AS truth
            FROM truth t1
            JOIN truth t2 ON t2.region = t1.region
                         AND t2.date = date(t1.date, '+' || ? || ' day')
        """
        w2, a2 = "", []
        if window_days:
            w2 = " WHERE t2.date >= ?"
            a2 = [_since(window_days)]
        for row in conn.execute(sql + w2, [h] + a2):
            out.append(scoring.Verif("baseline:persistence", row["region"], h,
                                     row["target_date"], row["forecast"], row["truth"]))
    return out


def entity_names(conn) -> dict[str, dict]:
    names: dict[str, dict] = dict(BASELINE_META)
    for code, meta in P.PRODUCTS.items():
        names[f"product:{code}"] = {
            "name": meta["name"], "kind": "product",
            "org": meta.get("org", ""), "note": meta.get("note", ""),
        }
    for row in conn.execute("SELECT id, name, kind, affiliation FROM players"):
        names[f"player:{row['id']}"] = {
            "name": row["name"], "kind": row["kind"],
            "org": row["affiliation"] or "", "note": "",
        }
    return names


def leaderboard(conn, window_days: int = 60, weights: dict | None = None,
                bias_tau: float = scoring.BIAS_TAU,
                hit_tol: float = scoring.HIT_TOL,
                common_sample: bool = True) -> dict:
    entries = load_entries(conn, window_days)
    names = entity_names(conn)

    # ---- 共同样本 ----
    # 各个条目的样本区间天然不一样：官方产品只覆盖最近几天，玩家可能只玩过一天，
    # 而持续性基线覆盖全部历史。直接比较会得出"样本少的人更准"的假象。
    # 做法和论文一致：在每个 (海区, 时效) 分组里，只保留所有官方产品都有数据的
    # 那些日期（"on the days both exist"）。交集太小（< 3 天）时退回全集，避免
    # 冷启动阶段把所有人都筛没。
    common_dates: dict[tuple[str, int], set[str]] = {}
    if common_sample:
        per_group: dict[tuple[str, int], dict[str, set[str]]] = {}
        for e in entries:
            if e.entity in REFERENCE_ENTITIES:
                per_group.setdefault((e.region, e.horizon), {}).setdefault(
                    e.entity, set()).add(e.target_date)
        for key, per in per_group.items():
            sets = list(per.values())
            inter = set.intersection(*sets) if sets else set()
            common_dates[key] = inter if len(inter) >= 3 else set.union(*sets)

        kept = []
        for e in entries:
            key = (e.region, e.horizon)
            cd = common_dates.get(key)
            if cd and e.target_date not in cd:
                continue
            kept.append(e)
        entries = kept

    ref = scoring.reference_from_products(entries, REFERENCE_ENTITIES)

    by_entity: dict[str, list[scoring.Verif]] = {}
    for e in entries:
        by_entity.setdefault(e.entity, []).append(e)

    rows = []
    for entity, recs in by_entity.items():
        m = scoring.score_entity(recs, ref, weights, bias_tau, hit_tol)
        meta = names.get(entity, {"name": entity, "kind": "unknown"})
        dates = sorted({r.target_date for r in recs})
        rows.append({
            "entity": entity,
            "name": meta["name"],
            "kind": meta["kind"],
            "org": meta.get("org", ""),
            "n": m.n,
            "n_dates": len(dates),
            "date_from": dates[0] if dates else None,
            "date_to": dates[-1] if dates else None,
            "mae": None if m.mae != m.mae else round(m.mae, 3),
            "rmse": None if m.rmse != m.rmse else round(m.rmse, 3),
            "bias": None if m.bias != m.bias else round(m.bias, 3),
            "hit": None if m.hit != m.hit else round(m.hit, 3),
            "advantage": None if m.advantage != m.advantage else round(m.advantage, 4),
            "score": None if m.score != m.score else round(m.score, 2),
        })
    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0), r["mae"] or 9))
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    # 分组基准表：每个 (海区, 时效) 官方最好的 MAE 是多少
    groups = []
    for (region, horizon), val in sorted(ref.items()):
        cd = common_dates.get((region, horizon))
        groups.append({
            "region": region,
            "region_cn": R.get(region).name_cn if region in R.BY_CODE else region,
            "horizon": horizon,
            "ref_mae": round(val, 3),
            "common_dates": len(cd) if cd else None,
        })
    return {"rows": rows, "groups": groups, "window_days": window_days,
            "common_sample": common_sample}


def entity_detail(conn, entity: str, window_days: int = 60) -> dict:
    entries = [e for e in load_entries(conn, window_days) if e.entity == entity]
    names = entity_names(conn)
    ref = scoring.reference_from_products(load_entries(conn, window_days), REFERENCE_ENTITIES)
    m = scoring.score_entity(entries, ref)
    meta = names.get(entity, {"name": entity, "kind": "unknown"})
    return {
        "entity": entity,
        "name": meta["name"],
        "kind": meta["kind"],
        "n": m.n,
        "mae": None if m.mae != m.mae else round(m.mae, 3),
        "rmse": None if m.rmse != m.rmse else round(m.rmse, 3),
        "bias": None if m.bias != m.bias else round(m.bias, 3),
        "hit": None if m.hit != m.hit else round(m.hit, 3),
        "advantage": None if m.advantage != m.advantage else round(m.advantage, 4),
        "score": None if m.score != m.score else round(m.score, 2),
        "by_horizon": m.by_horizon,
    }


def recent_settled(conn, limit: int = 30) -> list[dict]:
    rows = []
    sql = """
        SELECT t.date, t.region, t.sst, t.source,
               (SELECT COUNT(*) FROM submissions s
                 WHERE s.target_date = t.date AND s.region = t.region) AS n_subs
        FROM truth t ORDER BY t.date DESC, t.region
        LIMIT ?
    """
    for r in conn.execute(sql, (limit,)):
        rows.append({
            "date": r["date"], "region": r["region"],
            "region_cn": R.get(r["region"]).name_cn if r["region"] in R.BY_CODE else r["region"],
            "sst": r["sst"], "source": r["source"], "n_submissions": r["n_subs"],
        })
    return rows


def truth_series(conn, days: int = 60, region: str | None = None) -> list[dict]:
    since = _since(days)
    if region:
        cur = conn.execute(
            "SELECT date, region, sst FROM truth WHERE date>=? AND region=? ORDER BY date",
            (since, region),
        )
    else:
        cur = conn.execute(
            "SELECT date, region, sst FROM truth WHERE date>=? ORDER BY date, region",
            (since,),
        )
    return [{"date": r["date"], "region": r["region"], "sst": r["sst"]} for r in cur]


def products_for(conn, target_date: str) -> list[dict]:
    rows = []
    sql = """
        SELECT product, region, horizon, run_date, value
        FROM products WHERE target_date = ?
        ORDER BY horizon, product, region
    """
    for r in conn.execute(sql, (target_date,)):
        meta = P.PRODUCTS.get(r["product"], {})
        rows.append({
            "product": r["product"], "name": meta.get("name", r["product"]),
            "org": meta.get("org", ""),
            "region": r["region"], "horizon": r["horizon"],
            "run_date": r["run_date"], "value": r["value"],
        })
    return rows


# ------------------------------------------------------------------ 进程内缓存

_cache: dict[str, tuple[float, object]] = {}


def cached(key: str, ttl: float, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def invalidate() -> None:
    _cache.clear()


# ------------------------------------------------------------------ 极端值

def _thresholds() -> dict:
    """每个海区、每个 day-of-year 的 90 分位阈值。"""
    clim = mhw.load_climatology()
    return clim["regions"]


def extreme_report(conn, window_days: int = 180) -> dict:
    """把每个条目的成绩按"是否热浪日"拆开，并单独检验热浪检测能力。

    为什么值得单独看：极端事件才是预报真正有代价的地方。
    一个模型平时 MAE 很漂亮，可能一到热浪就失灵——那它在实际使用中几乎没用。
    """
    saved = mhw.load_saved() or {}
    hot = mhw.mhw_day_set(saved)
    clim = _thresholds()
    since = _since(window_days)

    entries = load_entries(conn, window_days)
    names = entity_names(conn)

    # 每条的：是否热浪日、预报是否越过阈值、真值是否越过阈值
    by_entity: dict[str, dict] = {}
    for e in entries:
        if e.target_date < since:
            continue
        k = G_doy(e.target_date)
        thr = clim.get(e.region, {}).get("p90", [None] * 365)
        t = thr[k] if thr and k < len(thr) else None
        if t is None:
            continue
        is_hot = (e.region, e.target_date) in hot
        row = by_entity.setdefault(e.entity, {
            "err_hot": [], "err_norm": [], "truth_hot": 0, "truth_norm": 0,
            "hit": 0, "miss": 0, "fa": 0, "cr": 0,
        })
        err = e.forecast - e.truth
        if is_hot:
            row["err_hot"].append(err)
            row["truth_hot"] += 1
        else:
            row["err_norm"].append(err)
            row["truth_norm"] += 1
        pred_hot = e.forecast > t
        if pred_hot and is_hot:
            row["hit"] += 1
        elif pred_hot and not is_hot:
            row["fa"] += 1
        elif not pred_hot and is_hot:
            row["miss"] += 1
        else:
            row["cr"] += 1

    def mae(v):
        return round(sum(abs(x) for x in v) / len(v), 3) if v else None

    def rmse(v):
        return round((sum(x * x for x in v) / len(v)) ** 0.5, 3) if v else None

    rows = []
    for entity, r in by_entity.items():
        n_hot = len(r["err_hot"])
        n_norm = len(r["err_norm"])
        if n_hot + n_norm < 10:
            continue
        hits, miss, fa = r["hit"], r["miss"], r["fa"]
        meta = names.get(entity, {"name": entity, "kind": "unknown"})
        rows.append({
            "entity": entity, "name": meta["name"], "kind": meta["kind"],
            "n_hot": n_hot, "n_norm": n_norm,
            "mae_hot": mae(r["err_hot"]), "mae_norm": mae(r["err_norm"]),
            "rmse_hot": rmse(r["err_hot"]), "rmse_norm": rmse(r["err_norm"]),
            "bias_hot": (round(sum(r["err_hot"]) / n_hot, 3) if n_hot else None),
            "degrade": (round(mae(r["err_hot"]) / mae(r["err_norm"]) - 1, 3)
                        if n_hot and n_norm and mae(r["err_norm"]) else None),
            "pod": round(hits / (hits + miss), 3) if hits + miss else None,
            "far": round(fa / (hits + fa), 3) if hits + fa else None,
        })
    rows.sort(key=lambda z: (z["mae_hot"] is None, z["mae_hot"] or 9))

    # 热浪日的总体占比，给前端做说明
    tot_hot = sum(r["n_hot"] for r in rows) or 0
    tot_norm = sum(r["n_norm"] for r in rows) or 0
    return {
        "window_days": window_days,
        "n_hot": tot_hot, "n_norm": tot_norm,
        "hot_ratio": round(tot_hot / (tot_hot + tot_norm), 3) if tot_hot + tot_norm else None,
        "rows": rows,
        "baseline": saved.get("baseline"),
        "series_range": saved.get("series_range"),
    }


def G_doy(day: str) -> int:
    from . import grid as _G

    return _G.doy_index(day)
