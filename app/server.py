"""Web 服务（纯标准库）。

设计沿用主站约定：
  · 只监听 127.0.0.1，外面由 cloudflared 隧道兜
  · 配置全部来自 data/config.json
  · 网页 / 接口 / 数据都在一个进程里，靠 URL 前缀区分
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import socketserver
import sys
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, db, products as P, service
from . import ingest_file, maps
from . import regions as R

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(BASE, "web")
PORTAL = os.path.join(WEB, "portal")

# 这些域名打过来时给"总入口"页，其余域名给海温擂台。
# 和主站同一套约定：一个进程、一个端口，靠 Host 头分站。
PORTAL_HOSTS = {"www.playai.org.cn", "playai.org.cn"}

CFG = config.load()
START = time.time()

_rate: dict[str, list[float]] = {}


def _rate_ok(key: str, limit: int, window: float = 3600.0) -> bool:
    now = time.time()
    hits = [t for t in _rate.get(key, []) if now - t < window]
    if len(hits) >= limit:
        _rate[key] = hits
        return False
    hits.append(now)
    _rate[key] = hits
    return True


class Handler(BaseHTTPRequestHandler):
    server_version = "SSTPrediction/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 基础
    def log_message(self, fmt, *args):  # noqa: A003
        sys.stderr.write(
            f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} "
            f"{self.address_string()} {fmt % args}\n"
        )

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _err(self, code: int, msg: str):
        self._json({"error": msg}, code)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _read_raw(self, limit: int = 120_000_000) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > limit:
            return b""
        return self.rfile.read(n)

    @staticmethod
    def _parse_multipart(body: bytes, boundary: str):
        """极简 multipart/form-data 解析（标准库的 cgi 模块已经没了）。"""
        fields: dict[str, str] = {}
        files: list[tuple[str, bytes]] = []
        sep = b"--" + boundary.encode()
        for part in body.split(sep):
            if not part or part in (b"--\r\n", b"--"):
                continue
            part = part.lstrip(b"\r\n")
            head_end = part.find(b"\r\n\r\n")
            if head_end < 0:
                continue
            head = part[:head_end].decode("utf-8", "replace")
            content = part[head_end + 4:]
            if content.endswith(b"\r\n"):
                content = content[:-2]
            m = re.search(r'name="([^"]*)"', head)
            if not m:
                continue
            name = m.group(1)
            fn = re.search(r'filename="([^"]*)"', head)
            if fn and fn.group(1):
                files.append((fn.group(1), content))
            else:
                fields[name] = content.decode("utf-8", "replace")
        return fields, files

    def _static(self, path: str, root: str | None = None):
        root = root or WEB
        rel = path.lstrip("/") or "index.html"
        if root == PORTAL:
            # 总入口只放行自己的两个静态文件，其余路径退回擂台站
            if rel not in ("portal.css", "index.html", "favicon.ico"):
                return self._send(302, b"", "text/plain", {"Location": "/"})
        # 只放行一个数据文件（模型验证结果），其余 data/ 一律不对外
        if rel == "data/model_validation.json":
            full = os.path.join(BASE, "data", "model_validation.json")
            if os.path.isfile(full):
                with open(full, "rb") as f:
                    self._send(200, f.read(), "application/json; charset=utf-8")
            else:
                self._send(404, b"{}", "application/json; charset=utf-8")
            return
        full = os.path.normpath(os.path.join(root, rel))
        if not full.startswith(root) or not os.path.isfile(full):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            body = f.read()
        self._send(200, body, ctype)

    # ------------------------------------------------------------ 路由
    def do_GET(self):  # noqa: N802
        try:
            self._route_get()
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._err(500, "internal error")

    do_HEAD = do_GET

    def do_POST(self):  # noqa: N802
        try:
            self._route_post()
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._err(500, "internal error")

    def _route_get(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        q = urllib.parse.parse_qs(u.query)
        q1 = {k: v[0] for k, v in q.items()}

        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host in PORTAL_HOSTS:
            if path == "/healthz":
                return self._json({"ok": True, "site": "portal"})
            if path in ("/", ""):
                return self._static("/index.html", PORTAL)
            if path.startswith("/portal.css"):
                return self._static("/portal.css", PORTAL)
            return self._send(404, b"not found", "text/plain; charset=utf-8")

        if path == "/api/meta":
            return self._json(self._meta())
        if path == "/api/rounds":
            return self._json(self._rounds(q1.get("token")))
        if path == "/api/truth":
            with db.session() as conn:
                return self._json(service.truth_series(
                    conn, int(q1.get("days", 60)), q1.get("region")))
        if path == "/api/products":
            with db.session() as conn:
                return self._json(service.products_for(conn, q1.get("target", "")))
        if path == "/api/map":
            return self._map(q1)
        if path == "/api/model_report":
            return self._json(self._model_report())
        if path == "/api/leaderboard":
            return self._json(self._leaderboard(int(q1.get("days", 0)) or None))
        if path.startswith("/api/entity/"):
            code = urllib.parse.unquote(path[len("/api/entity/"):])
            with db.session() as conn:
                return self._json(service.entity_detail(conn, code))
        if path == "/api/me":
            return self._json(self._me(q1.get("token", "")))
        if path == "/api/admin/status":
            return self._json(self._admin_status(q1.get("token", "")))
        if path.startswith("/api/"):
            return self._err(404, "unknown api")
        if path == "/healthz":
            return self._json({"ok": True, "uptime": round(time.time() - START, 1)})
        return self._static(path)

    def _route_post(self):
        u = urllib.parse.urlparse(self.path)
        # 上传接口必须最先处理：它的 body 是 multipart，不能让下面那句
        # _read_json() 先把请求体读走，否则 _upload 再读会一直阻塞到客户端超时。
        if u.path == "/api/upload":
            return self._upload()
        body = self._read_json()
        if u.path == "/api/register":
            return self._register(body)
        if u.path == "/api/submit":
            return self._submit(body)
        if u.path == "/api/admin/invalidate":
            if body.get("token") != CFG["admin_token"]:
                return self._err(403, "bad admin token")
            service.invalidate()
            return self._json({"ok": True})
        return self._err(404, "unknown api")

    # ------------------------------------------------------------ 处理
    def _meta(self) -> dict:
        sc = CFG["scoring"]
        return {
            "site_name": CFG["site_name"],
            "domain": CFG["domain"],
            "regions": [r.as_dict() for r in R.REGIONS],
            "horizons": list(R.HORIZONS),
            "products": P.PRODUCTS,
            "scoring": {
                "weights": sc["weights"],
                "bias_tau": sc["bias_tau"],
                "hit_tol": sc["hit_tol"],
                "min_n": 10,
                "reference": "同一海区、同一时效下表现最好的官方预报产品（HYCOM / GFS / CFSv2）",
            },
            "truth_source": "NOAA OISST v2.1（daily 0.25°，近实时版滞后约 1–2 天）",
        }

    # ------------------------------------------------------------ 海温图
    def _model_report(self) -> dict:
        """把本站各模型的验证结果汇总给前端（都是本地生成的 json）。"""
        out: dict = {"lim": None, "postproc": None, "unet": None}
        files = {
            "lim": os.path.join(BASE, "data", "model_validation.json"),
            "postproc": os.path.join(BASE, "data", "models", "postproc.json"),
            "unet": os.path.join(BASE, "data", "models", "unet.meta.json"),
        }
        for k, p in files.items():
            try:
                with open(p, encoding="utf-8") as f:
                    out[k] = json.load(f)
            except Exception:  # noqa: BLE001
                out[k] = None
        # 场库积累情况：多源通道什么时候能打开，就看这些数字
        try:
            from . import fieldstore

            out["fields"] = {
                p: {"days": len(fieldstore.list_dates(p)),
                    "files": len(fieldstore.load_index(p))}
                for p in ("gfs", "hycom", "cfs")
            }
        except Exception:  # noqa: BLE001
            out["fields"] = None
        return out

    def _map(self, q1: dict):
        kind = q1.get("kind", "truth")
        day = q1.get("date") or q1.get("target") or ""
        h = int(q1.get("horizon", 1) or 1)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return self._err(400, "需要 date=YYYY-MM-DD")
        try:
            got = maps.save(kind, day, h)
        except Exception as e:  # noqa: BLE001
            return self._err(500, f"绘图失败：{e}")
        if not got:
            return self._err(404, "这个日期的场还不存在")
        png_bytes, vmin, vmax = got
        self._send(200, png_bytes, "image/png",
                   {"X-Scale-Min": str(vmin), "X-Scale-Max": str(vmax),
                    "Cache-Control": "public, max-age=300"})

    # ------------------------------------------------------------ 文件提交
    def _upload(self):
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r'boundary=("?)([^";]+)\1', ctype)
        if "multipart/form-data" not in ctype or not m:
            return self._err(400, "请用 multipart/form-data 上传")
        body = self._read_raw()
        if not body:
            return self._err(400, "文件为空或超过 120MB")
        fields, files = self._parse_multipart(body, m.group(2))
        if not files:
            return self._err(400, "没有收到文件")

        name = (fields.get("name") or "").strip()
        if not (2 <= len(name) <= 40):
            return self._err(400, "请填写 2–40 字的模型/队伍名")
        horizon = None
        if fields.get("horizon"):
            try:
                horizon = int(fields["horizon"])
            except ValueError:
                horizon = None
        allow_late = fields.get("allow_late") in ("1", "true", "yes")

        filename, raw = files[0]
        try:
            entries, fmt = ingest_file.parse_upload(filename, raw, horizon)
        except ingest_file.IngestError as e:
            return self._err(400, f"解析失败：{e}")
        if not entries:
            return self._err(400, "文件解析出来是空的，请检查列名与日期格式")

        token = fields.get("token") or ""
        now = datetime.now(timezone.utc)
        written, rejected = 0, []
        with db.session() as conn:
            me = db.player_by_token(conn, token) if token else None
            if me is None:
                dup = conn.execute(
                    "SELECT id, token FROM players WHERE name=? AND kind='model'", (name,)
                ).fetchone()
                if dup:
                    me = {"id": dup["id"], "token": dup["token"], "name": name}
                else:
                    created = db.create_player(conn, name, "model",
                                               fields.get("affiliation") or "")
                    me = created
            for e in entries:
                if not allow_late and not db.is_open(e["target_date"], e["horizon"], at=now):
                    rejected.append({"target_date": e["target_date"], "horizon": e["horizon"],
                                     "why": "已过截止"})
                    continue
                conn.execute(
                    "INSERT INTO submissions(player_id, region, horizon, target_date,"
                    " value, comment, created_at) VALUES(?,?,?,?,?,?,?)"
                    " ON CONFLICT(player_id, region, horizon, target_date) DO UPDATE SET"
                    "   value=excluded.value, comment=excluded.comment,"
                    "   created_at=excluded.created_at",
                    (me["id"], e["region"], e["horizon"], e["target_date"], e["value"],
                     (f"上传 {filename}" + ("｜回测" if allow_late else ""))[:120],
                     db.now_iso()),
                )
                written += 1
        service.invalidate()
        with db.session() as conn:
            score = service.entity_detail(conn, f"player:{me['id']}")
        return self._json({
            "ok": True,
            "player": {"id": me["id"], "name": name, "token": me["token"], "kind": "model"},
            "format": fmt,
            "filename": filename,
            "written": written,
            "rejected": rejected[:20],
            "n_rejected": len(rejected),
            "summary": ingest_file.summarise(entries),
            "score": score,
            "howto": ("以后想每天自动提交，用 POST /api/upload 带 token 和文件即可；"
                      "也可以命令行 python3 -m tools.submit_file"),
        })

    def _rounds(self, token: str | None) -> dict:
        now = datetime.now(timezone.utc)
        rounds = db.open_rounds(days_ahead=8, now=now)
        with db.session() as conn:
            counts = {}
            for r in conn.execute(
                "SELECT target_date, horizon, region, COUNT(*) n FROM submissions"
                " GROUP BY target_date, horizon, region"
            ):
                counts[(r["target_date"], r["horizon"], r["region"])] = r["n"]

            my: dict[tuple, float] = {}
            me = None
            if token:
                me = db.player_by_token(conn, token)
                if me:
                    for r in conn.execute(
                        "SELECT target_date, horizon, region, value FROM submissions"
                        " WHERE player_id=? AND target_date>=?",
                        (me["id"], (now.date() - timedelta(days=1)).isoformat()),
                    ):
                        my[(r["target_date"], r["horizon"], r["region"])] = r["value"]

            latest_truth = conn.execute("SELECT MAX(date) d FROM truth").fetchone()["d"]

        for rd in rounds:
            rd["counts"] = {
                reg: counts.get((rd["target_date"], rd["horizon"], reg), 0)
                for reg in R.all_codes()
            }
            rd["mine"] = {
                reg: my.get((rd["target_date"], rd["horizon"], reg))
                for reg in R.all_codes()
                if (rd["target_date"], rd["horizon"], reg) in my
            }
            rd["deadline"] = db.deadline_utc(rd["target_date"], rd["horizon"])
        return {
            "now": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "latest_truth": latest_truth,
            "me": {"name": me["name"], "id": me["id"], "kind": me["kind"]} if me else None,
            "rounds": rounds,
        }

    def _leaderboard(self, days: int | None) -> dict:
        sc = CFG["scoring"]
        days = days or sc["leaderboard_window_days"]

        def build():
            with db.session() as conn:
                return service.leaderboard(
                    conn, days, sc["weights"], sc["bias_tau"], sc["hit_tol"]
                )

        return service.cached(f"lb:{days}", 30.0, build)

    def _me(self, token: str) -> dict:
        if not token:
            return {"error": "missing token"}
        with db.session() as conn:
            me = db.player_by_token(conn, token)
            if not me:
                return {"error": "unknown token"}
            subs = [
                dict(r) for r in conn.execute(
                    "SELECT s.region, s.horizon, s.target_date, s.value, s.created_at,"
                    "       t.sst AS truth"
                    " FROM submissions s"
                    " LEFT JOIN truth t ON t.region=s.region AND t.date=s.target_date"
                    " WHERE s.player_id=? ORDER BY s.target_date DESC, s.horizon LIMIT 500",
                    (me["id"],),
                )
            ]
            detail = service.entity_detail(conn, f"player:{me['id']}")
        return {"player": {"name": me["name"], "id": me["id"], "kind": me["kind"]},
                "submissions": subs, "score": detail}

    def _register(self, body: dict) -> None:
        name = (body.get("name") or "").strip()
        if not (2 <= len(name) <= 40):
            return self._err(400, "名字长度需要在 2–40 字之间")
        kind = body.get("kind") if body.get("kind") in ("human", "team") else "human"
        with db.session() as conn:
            dup = conn.execute("SELECT id FROM players WHERE name=? AND kind=?",
                               (name, kind)).fetchone()
            if dup:
                return self._err(409, "这个名字已经被用了，换一个吧")
            p = db.create_player(conn, name, kind, body.get("affiliation") or "")
        service.invalidate()
        return self._json({"ok": True, "player": p})

    def _submit(self, body: dict) -> None:
        token = body.get("token") or ""
        entries = body.get("entries") or []
        if not token:
            return self._err(401, "缺少 token")
        limit = CFG["rate_limit"]["submits_per_hour"]
        if not _rate_ok(f"submit:{token}", limit):
            return self._err(429, "提交太频繁，请稍后再试")

        now = datetime.now(timezone.utc)
        written, rejected = 0, []
        with db.session() as conn:
            me = db.player_by_token(conn, token)
            if not me:
                return self._err(401, "token 无效")
            for e in entries:
                region = e.get("region")
                try:
                    horizon = int(e.get("horizon"))
                    value = float(e.get("value"))
                except (TypeError, ValueError):
                    rejected.append({"entry": e, "why": "格式错误"})
                    continue
                target = str(e.get("target_date") or "")
                if region not in R.BY_CODE:
                    rejected.append({"entry": e, "why": "未知海区"})
                    continue
                if horizon not in R.HORIZONS:
                    rejected.append({"entry": e, "why": "未知时效"})
                    continue
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target):
                    rejected.append({"entry": e, "why": "日期格式错误"})
                    continue
                if not (-5.0 <= value <= 45.0):
                    rejected.append({"entry": e, "why": "温度超出合理范围"})
                    continue
                if not db.is_open(target, horizon, at=now):
                    rejected.append({"entry": e, "why": "该轮已截止"})
                    continue
                conn.execute(
                    "INSERT INTO submissions(player_id, region, horizon, target_date,"
                    " value, comment, created_at) VALUES(?,?,?,?,?,?,?)"
                    " ON CONFLICT(player_id, region, horizon, target_date) DO UPDATE SET"
                    "   value=excluded.value, comment=excluded.comment,"
                    "   created_at=excluded.created_at",
                    (me["id"], region, horizon, target, round(value, 2),
                     (e.get("comment") or "")[:120], db.now_iso()),
                )
                written += 1
        service.invalidate()
        return self._json({"ok": True, "written": written, "rejected": rejected})

    def _admin_status(self, token: str) -> dict:
        if token != CFG["admin_token"]:
            return {"error": "bad admin token"}
        with db.session() as conn:
            latest = conn.execute("SELECT MAX(date) d, COUNT(DISTINCT date) nd FROM truth").fetchone()
            subs = conn.execute("SELECT COUNT(*) n FROM submissions").fetchone()["n"]
            pl = conn.execute("SELECT COUNT(*) n FROM players WHERE kind!='product'").fetchone()["n"]
            prod = conn.execute(
                "SELECT product, COUNT(*) n, MAX(target_date) mx FROM products GROUP BY product"
            )
            prods = [{"product": r["product"], "rows": r["n"], "latest_target": r["mx"]}
                     for r in prod]
            truth_days = [r["date"] for r in conn.execute(
                "SELECT DISTINCT date FROM truth ORDER BY date DESC LIMIT 10")]
        return {
            "latest_truth": latest["d"], "truth_days": latest["nd"],
            "recent_truth_dates": truth_days,
            "submissions": subs, "players": pl, "products": prods,
            "uptime_seconds": round(time.time() - START, 1),
        }


class Server(ThreadingHTTPServer):
    """默认的 HTTPServer.server_bind() 会对本机做一次反向 DNS（socket.getfqdn）。

    这台机器的 DNS 走代理，那次反查会卡十几到几十秒，表现成"服务启动了但连不上"。
    HTTP 服务根本不需要 FQDN，直接跳过。
    """

    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def main() -> int:
    db.init()
    port = int(os.environ.get("SST_PORT", CFG["port"]))
    host = os.environ.get("SST_HOST", "127.0.0.1")
    httpd = Server((host, port), Handler)
    print(f"{CFG['site_name']} 已启动：http://{host}:{port}/  （admin token 在 data/config.json）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
