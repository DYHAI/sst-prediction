"""配置外置：所有"换环境就要变"的东西都进 data/config.json。"""

from __future__ import annotations

import json
import os
import secrets

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
CONFIG_PATH = os.path.join(DATA, "config.json")

DEFAULTS: dict = {
    "port": 8770,
    "site_name": "南海海温预测擂台",
    "domain": "sst.playai.org.cn",
    "admin_token": "",
    "scoring": {
        "weights": {"acc": 0.45, "bias": 0.20, "hit": 0.35},
        "bias_tau": 0.5,
        "hit_tol": 0.5,
        "leaderboard_window_days": 60,
    },
    "rate_limit": {"submits_per_hour": 60},
}


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception:  # noqa: BLE001
            pass
    if not cfg.get("admin_token"):
        cfg["admin_token"] = secrets.token_urlsafe(18)
        save(cfg)
    return cfg


def save(cfg: dict) -> None:
    os.makedirs(DATA, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
