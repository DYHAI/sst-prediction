#!/usr/bin/env python3
"""自动批准 OpenClaw 的设备配对请求（跑在 openclaw.playai.org.cn 上的网关）。

为什么需要它
------------
网关只监听 127.0.0.1，外面由 Cloudflare 隧道转发。对网关来说，这种请求带
X-Forwarded-* 头，属于"远端"（locality=remote），于是 OpenClaw 不会走
"本机静默配对"的快捷路径，任何新浏览器第一次连上来都会停在
"Gateway pairing approval required"，必须有人手动批一次。

安全边界（重要）
----------------
配对请求只有在 **token 校验通过之后** 才会生成，所以这个脚本等于把安全
边界从"token + 设备批准"降成"只有 token"。
本机开着 shell 工具，拿到 token 的人可以借 agent 在 Mac 上执行命令，
请把 token 当密码保管。

想手动批准就删掉开关文件，或者直接 `touch` 下面这个路径：
    data/openclaw_autoapprove.disabled
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
KILL_SWITCH = PROJECT_DIR / "data" / "openclaw_autoapprove.disabled"
LOG_PATH = Path(os.environ.get("OPENCLAW_AUTOAPPROVE_LOG", Path.home() / "Library" / "Logs" / "openclaw-autoapprove.log"))
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", "/opt/homebrew/bin/openclaw")

# Control UI 首次连接是按 Fp 这个默认集合申请的：
#   operator.admin / read / write / approvals / questions / pairing
# `openclaw devices approve` 是按请求原样批准，没法只批一部分，
# 所以想让人进得来，就必须把这一整套都放行。
#
# 注意 operator.admin 的含义：能改配置、装插件（= 持久化）。
# 但这不是主要风险面——agent 默认就是 full 文件/exec 权限，
# 拿到 operator.write 已经等于能在这台 Mac 上跑命令了。
# 真正的边界是 token 本身，见 deploy/README.md 的说明。
AUTO_APPROVE_ROLES = {"operator"}
AUTO_APPROVE_SCOPES = {
    "operator.admin",
    "operator.read",
    "operator.write",
    "operator.sessions.read",
    "operator.sessions.write",
    "operator.talk",
    "operator.questions",
    "operator.approvals",
    "operator.pairing",
}


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def run_openclaw(args: list[str], timeout: int = 25) -> str:
    env = dict(os.environ)
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    completed = subprocess.run(
        [OPENCLAW_BIN, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(Path.home()),
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"exit {completed.returncode}")
    return completed.stdout


def pending_requests() -> list[dict]:
    raw = run_openclaw(["devices", "list", "--json"])
    start = raw.find("{")
    return json.loads(raw[start:]).get("pending", [])


def main() -> int:
    if KILL_SWITCH.exists():
        return 0

    try:
        pending = pending_requests()
    except Exception as error:  # 网关没起来 / CLI 报错，下一轮再试
        log(f"读取待批准设备失败：{error}")
        return 0

    for request in pending:
        request_id = str(request.get("requestId") or "").strip()
        roles = set(request.get("roles") or [])
        scopes = set(request.get("scopes") or [])
        if not request_id:
            continue

        blocked = (roles - AUTO_APPROVE_ROLES) or (scopes - AUTO_APPROVE_SCOPES)
        sensitive = "operator.admin" in scopes
        if blocked:
            log(
                f"跳过 {request_id}：超出自动批准范围 "
                f"roles={sorted(roles)} scopes={sorted(scopes)} "
                f"（需要人工 `openclaw devices approve {request_id}`）"
            )
            continue

        try:
            run_openclaw(["devices", "approve", request_id])
            log(
                f"{'已批准（含 admin，敏感）' if sensitive else '已批准'} {request_id} "
                f"device={str(request.get('deviceId'))[:16]} "
                f"ip={request.get('remoteIp')} scopes={sorted(scopes)}"
            )
        except Exception as error:
            log(f"批准 {request_id} 失败：{error}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
