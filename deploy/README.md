# 部署说明

目标：让这台 Mac 变成服务机，`https://sst.playai.org.cn` 常驻在线。

整体沿用主站架构，没有任何新东西：

```
玩家手机 / 电脑
      │  https://sst.playai.org.cn
      ▼
 Cloudflare 边缘节点（DNS + HTTPS + 防护 + 隐藏真实 IP）
      │  加密隧道（不需要公网 IP）
      ▼
 这台 Mac 上的 cloudflared
      │  http://127.0.0.1:8770
      ▼
 app.server（Python 标准库）
      ├── 网页 + 接口
      ├── 读官产品/真值（抓数由 launchd 定时任务负责）
      └── 本地数据：data/sst.db + data/config.json
```

---

## 第一步：目录位置（已经处理好了）

macOS 把 `~/Desktop`、`~/Documents`、`~/Downloads` 列为受保护目录。
launchd 拉起的进程默认没有访问权限，实测结果是**直接卡死**（不报错、不写日志，
连探针日志都写不出来）：

```console
# 最小复现：launchd 跑一个只做 os.listdir 的 python
$ cat /tmp/tcc_probe.log
cat: No such file or directory      # 进程卡在 TCC 授权上，永远写不出来
```

**当前采用的方案**：项目实体放在不受保护的位置，桌面上留一个符号链接。

```
/Users/dingding/srv/SST_Prediction          ← 真实目录（launchd 能访问）
/Users/dingding/Desktop/Program/SST_Prediction → 符号链接，指向上面
```

两条路径都能正常用；launchd 走的是 `~/srv` 那条。想改回去的话：
把 `~/srv/SST_Prediction` 挪回桌面，然后给 `/opt/homebrew/bin/python3`
开「完全磁盘访问权限」即可（见下面方案 A）。

### 解决办法（二选一）

**方案 A：给 Python 开完全磁盘访问权限（项目放桌面时需要）**

1. 打开 系统设置 → 隐私与安全性 → 完全磁盘访问权限
2. 点左下角 `+`，按 `⌘⇧G` 输入 `/opt/homebrew/bin/`
3. 选中 `python3`（如果显示的是别名，右键"显示原项目"，选真正的可执行文件），加入并打开开关
4. 重新加载服务：

```bash
launchctl unload ~/Library/LaunchAgents/com.playai.sst.plist
launchctl load   ~/Library/LaunchAgents/com.playai.sst.plist
curl -s http://127.0.0.1:8770/healthz     # 应返回 {"ok": true, ...}
```

**方案 B：把项目挪出受保护目录（当前实际使用的方案）**

```bash
mkdir -p ~/srv
mv /Users/dingding/Desktop/Program/SST_Prediction ~/srv/SST_Prediction
ln -s ~/srv/SST_Prediction /Users/dingding/Desktop/Program/SST_Prediction
# 把 deploy/*.plist 与 deploy/ingest.sh 里的路径一并改掉
sed -i '' 's|/Users/dingding/Desktop/Program/SST_Prediction|/Users/dingding/srv/SST_Prediction|g' \
    deploy/*.plist deploy/ingest.sh
```

> 验证权限是否已通：`deploy/` 里没有测试脚本，可以临时用
> `launchctl kickstart -k gui/$(id -u)/com.playai.sst` 后看 `logs/server.log` 有没有输出。

---

## 第二步：安装常驻服务（三个）

```bash
cd /Users/dingding/srv/SST_Prediction
mkdir -p logs

cp deploy/com.playai.sst.plist        ~/Library/LaunchAgents/
cp deploy/com.playai.sst.ingest.plist ~/Library/LaunchAgents/
cp deploy/com.playai.sst.tunnel.plist ~/Library/LaunchAgents/

launchctl load ~/Library/LaunchAgents/com.playai.sst.plist
launchctl load ~/Library/LaunchAgents/com.playai.sst.ingest.plist
launchctl load ~/Library/LaunchAgents/com.playai.sst.tunnel.plist

# 查看状态
launchctl list | grep sst
curl -s http://127.0.0.1:8770/healthz
```

两个服务分别做什么：

| plist | 作用 | 触发 |
|---|---|---|
| `com.playai.sst` | Web 服务，`KeepAlive=true`，崩了自动重启 | 开机 + 常驻 |
| `com.playai.sst.tunnel` | cloudflared 隧道，把 `sst.playai.org.cn` 打到 8770 | 开机 + 常驻 |
| `com.playai.sst.ingest` | 抓真值 + 抓官方产品 + 清缓存 | 每天北京时间 08:30 / 20:30 |
| `com.playai.sst.llm` | 本地大模型（Bonsai 2 27B，GPU 推理，端口 8780） | 开机 + 常驻 |

### 本地大模型服务

网页上的「问本地大模型」聊天框，后端是 `llama-server` + Bonsai 2 27B：

```bash
# 模型（5.95 GB，三元量化 1.75 bit）
~/models/Ternary-Bonsai-2-27B-PTQ1_0.gguf

# 需要厂商的 llama.cpp 分支（三元核，upstream 不支持 ggml type 143）
git clone https://github.com/PrismML-Eng/llama.cpp   # 国内需走代理
cd llama.cpp && cmake -B build -DGGML_METAL=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build -j 12

# 服务（已由 launchd 托管，见 com.playai.sst.llm.plist）
llama-server -m <模型> -ngl 99 -c 4096 -t 8 --host 127.0.0.1 --port 8780 --no-webui -np 2
```

网站把 `/api/chat` 转发到 8780，**边生成边推给前端（SSE）**，并按 IP 限流
（每小时 15 次，配置在 `data/config.json` 的 `chat` 段）。

> ⚠️ **踩过的坑：必须给这个转发显式禁用代理。**
> 这台机器开着系统级 HTTP 代理（FlClash `127.0.0.1:7890`）。
> 交互式 shell 里有 `no_proxy` 所以本地请求正常，但 launchd 起的服务没有这个变量，
> urllib 会去读 **macOS 系统代理设置**，把 `127.0.0.1:8780` 也塞进代理 → 一律 502。
> 代码里用 `build_opener(ProxyHandler({}))` 显式绕开。

抓数脚本是 `deploy/ingest.sh`，日志写在 `logs/ingest.log`。

常用运维命令：

```bash
launchctl kickstart -k gui/$(id -u)/com.playai.sst        # 重启网站
launchctl kickstart -k gui/$(id -u)/com.playai.sst.ingest # 立刻抓一次数
tail -f logs/server.log logs/ingest.log                   # 看日志
```

---

## 第三步：接 Cloudflare 隧道

```bash
brew install cloudflared

cloudflared tunnel login                    # 浏览器里授权 playai.org.cn
cloudflared tunnel create sst               # 记下输出的 UUID

# 指向本机 8770
cloudflared tunnel route dns sst sst.playai.org.cn
```

`~/.cloudflared/config.yml`：

```yaml
tunnel: 3db73b03-9aca-447a-b95b-7c480839429f
credentials-file: /Users/dingding/.cloudflared/3db73b03-9aca-447a-b95b-7c480839429f.json
protocol: http2          # 国内 UDP 7844 常被限速，走 TCP 上的 http2 更稳

ingress:
  - hostname: sst.playai.org.cn
    service: http://127.0.0.1:8770
  # 总入口页：同一个进程、同一个端口，靠 Host 头分站
  - hostname: www.playai.org.cn
    service: http://127.0.0.1:8770
  - hostname: playai.org.cn
    service: http://127.0.0.1:8770
  - service: http_status:404
```

对应地，`app/server.py` 里用 `PORTAL_HOSTS` 判断 Host：
`www.playai.org.cn` 和 `playai.org.cn` 给总入口页（`web/portal/`），
其它域名给海温擂台。加新站点只需要在隧道里加一行 + 在应用里加一个分支。

> 本机验证小插曲：这台 Mac 开着 Clash（`https_proxy=127.0.0.1:7890`），
> 新加的 `www` / 顶级域名一开始走代理会失败（`SSL_ERROR_SYSCALL`），
> 但 `curl --noproxy '*'` 正常、手机上访问也正常——是本地代理规则的问题，不是部署问题。

> 这台机器上跑的是**独立的 `sst` 隧道**（ID `3db73b03-…`），和主站那条 `mpa`
> 隧道（跑在另一台机器上）互不干扰。DNS 里 `sst.playai.org.cn` 是一条精确记录，
> 优先级高于可能存在的 `*.playai.org.cn` 泛解析，所以主站不受影响。

```bash
cloudflared tunnel run sst     # 先前台跑，确认能打开再交给 launchd
```

做成开机自启（推荐，避免 `sudo`）：

```bash
cp deploy/com.playai.sst.tunnel.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.playai.sst.tunnel.plist
```

日志在 `~/Library/Logs/sst-tunnel.err.log`。

---

## 第四步：防止 Mac 睡眠

隧道和应用都在本机，Mac 睡了网站就挂了。

```bash
sudo pmset -a sleep 0 disksleep 0
# 或者临时压住：caffeinate -s
```

---

## 验收清单

```bash
# 1. 本机应用
curl -s http://127.0.0.1:8770/healthz

# 2. 公网
curl -s https://sst.playai.org.cn/healthz

# 3. 数据是否在更新
curl -s http://127.0.0.1:8770/api/admin/status?token=<data/config.json 里的 admin_token>

# 4. 榜单
curl -s "http://127.0.0.1:8770/api/leaderboard?days=60" | head -c 400
```

`admin_token` 在 `data/config.json` 里（首次启动自动生成）。

---

## OpenClaw 接入本地 27B

[OpenClaw](https://openclaw.ai)（`openclaw/openclaw`，MIT，GitHub 39 万星）是一个
个人 AI 助手网关：一端接各家聊天平台（Discord / Telegram / iMessage / Slack…20+），
另一端接模型。它自带 `llama-cpp` 插件，**支持直接连一个已有的 llama-server**，
所以我们的 Bonsai 2 27B 可以直接接进去。

### 安装（不需要 sudo）

`/usr/local` 不可写，所以把 npm 前缀放到用户目录：

```bash
npm install -g --prefix ~/.npm-global \
  --allow-scripts=openclaw,@google/genai,esbuild,koffi,protobufjs \
  openclaw@latest
ln -sf ~/.npm-global/bin/openclaw /opt/homebrew/bin/openclaw
openclaw plugins install @openclaw/llama-cpp-provider
```

> npm 默认会拦下几个包的构建脚本，其中 **esbuild 是必须的**（不跑它的 postinstall 就起不来），
> 所以要用 `--allow-scripts` 显式放行。

### 配置（连接已有服务器，不让它自己管进程）

关键点：**不要写 `localService`**。文档说得很明确：
`models.providers.llama-cpp.localService` 存在 = OpenClaw 托管进程；
不存在 = `baseUrl` 指向一个已有端点。

```json5
// openclaw config patch --file <这个文件>
{
  models: { providers: { "llama-cpp": {
    baseUrl: "http://127.0.0.1:8780",
    models: [{
      id: "bonsai-27b", name: "Bonsai 2 27B (local, ternary)",
      reasoning: false, input: ["text"],
      contextWindow: 24576, maxTokens: 4096,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      compat: { supportsTools: true },
    }],
  }}},
}
```

```bash
openclaw models set llama-cpp/bonsai-27b                     # 设为默认模型
openclaw models auth paste-token --provider llama-cpp        # 喂个占位 token（服务不校验）
openclaw gateway install                                     # 装成 LaunchAgent，开机自启
```

### 为 Agent 调大上下文（重要）

OpenClaw 的系统提示词 + **54 个工具定义就有 12,474 token**，
按网站聊天那种 4K/8K 上下文会直接报 `Context overflow`。实测梯度：

| 总上下文 `-c` | 槽位 `-np` | 每槽 | 结果 |
|---|---|---|---|
| 8192 | 2 | 4096 | ✗ 溢出 |
| 16384 | 2 | 8192 | ✗ 溢出（实测 12,474 token） |
| 32768 | 2 | 16384 | △ 能跑但输出被截断 |
| **49152** | **2** | **24576** | ✓ 提示词 + 输出 + 多轮都够 |

内存代价：24K/槽时模型 + KV 合计约 **11 GB wired**，16 GB 机器上只剩 1.2–0.5 GB。

### 实测效果

| 任务 | 耗时 | 结果 |
|---|---|---|
| 单句问答（首轮，提示词未缓存） | 56 s | ✅ 正确 |
| 追问（提示词缓存命中） | **2.6 s** | ✅ 记得上一轮内容 |
| **调用 shell 工具列文件** | 18 s | ✅ 正确列出两个 gguf 及大小 |
| 网站聊天页（同一台 llama-server） | 6 s | ✅ 流式输出 |

> `llama-server` 用 `--reasoning off` 全局关掉了思考模式。这个模型默认会写一大段推理草稿，
> 在 18 t/s 的机器上一道题要两三分钟且常常写不完；关掉之后 agent 单轮延迟从分钟级降到几十秒。
