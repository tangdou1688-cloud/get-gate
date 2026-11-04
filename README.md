# VPNGate SSTP 家宽节点（edgetunnel 链式代理） 🚀

自动抓取 [VPN Gate](https://www.vpngate.net/) 的 SSTP 家宽/机房节点，调用检测 Worker 逐个验证可用性，按国家分组、标注住宅/机房，生成可直接通过 **URL 自动轮换** 的节点清单。**每小时自动更新一次。**

> 核心价值：VPN Gate 的 SSTP 节点 30 分钟就换一批，手动测试筛选太痛苦。本仓库把它全自动了——你只需把 `nodes.txt` 的网址填进 edgetunnel 后台一次，之后节点每小时自动换，零手动。

---

## 架构（数据流向）

~~~text
VPN Gate 官方源
      │  (每小时，GitHub Actions 定时抓取)
      ▼
筛选 SSTP 节点 → 去重
      │
      ▼
检测 Worker (CheckSocks5，部署在 Cloudflare)
      │  GET /check?sstp=vpn:vpn@host:port
      │  返回 success + 出口 IP(住宅/机房判定)
      ▼
保留成功节点 → 按国家分组 → 住宅/机房标注 → 延迟排序
      │
      ▼
生成 nodes.txt (GitHub Pages 发布)
      │
      ▼
edgetunnel 后台「自定义优选IP」框填 https://…/nodes.txt
      │  edgetunnel 每次生成订阅时自动 fetch → 解析 $sstp:// → 套链式代理
      ▼
客户端订阅 edgetunnel 订阅 → 使用 SSTP 家宽节点 (每小时自动换)
~~~

---

## 一、完整部署教程（从零开始）

### 前置条件
- 一个 Cloudflare 账号（免费即可）
- 一个 GitHub 账号
- 一个已转入 Cloudflare 的域名（可选，但强烈推荐）

### 第 1 步：部署 edgetunnel（核心使用端）

1. 登录 Cloudflare 控制台，点击左侧 **Workers 和 Pages**
2. 点击 **创建** → 选择 **创建 Worker**，起名 `edgetunnel`，点击 **部署**
3. 打开 https://github.com/cmliu/edgetunnel/blob/main/_worker.js ，复制全部代码
4. 回到 Cloudflare Worker 编辑器，粘贴代码，点击 **保存并部署**
5. 在 **设置** → **变量** 中，添加变量 `ADMIN`，值填你的管理员密码
6. 在 **绑定** 中，添加 KV 命名空间绑定，变量名称填 `KV`
7. （推荐）在 **触发器** 中绑定自定义域名
8. 浏览器访问 `https://你的域名/admin`，登录后台
9. **在后台首页记下你的 UUID 和节点域名**（后面要用）

> **关键**：`UUID` 和 `节点域名` 是 edgetunnel 自己的配置，**不需要**在 `vpngate.py` 中设置。`vpngate.py` 只负责生成 `nodes.txt`，edgetunnel 会用它自己的 UUID/域名去生成最终订阅。

### 第 2 步：部署检测 Worker（CheckSocks5）

1. 打开 https://github.com/lsh8848/cm-Workers-CheckSocks5 ，点 **Fork**
2. 进 Cloudflare 控制台 → Workers 和 Pages → 创建 → 创建 Worker
3. 把 `_worker.js` 的全部内容粘贴进编辑器，点「部署」
4. 记下这个 Worker 的域名，形如 `https://xxx.你的用户名.workers.dev`
5. 验证：浏览器打开 `https://你的Worker域名/check?sstp=vpn:vpn@任意节点:端口` ，能返回 JSON 即成功

### 第 3 步：Fork 本仓库

在 GitHub 上打开本仓库，点 **Fork**，复制到你账号下。

### 第 4 步：修改配置（重点）

进你 fork 的仓库，修改 `vpngate.py`：

| 文件 | 位置 | 改成什么 | 为什么 |
| :--- | :--- | :--- | :--- |
| GitHub Actions Secret | `DOMAIN` | 你的检测 Worker 域名，形如 `https://xxx.workers.dev/check?sstp=vpn:vpn@` | 检测统一走你自己的 Worker；**只放 Secret，不要提交进仓库** |
| vpngate.py | 无需改代码 | 站点地址由环境变量 `SITE_URL` 控制（默认 `https://<你的用户名>.github.io/get-gate`），fork 后设置 `SITE_URL` 或改 `config.py` 的 `DEFAULT_SITE_URL` | 自动更新时用到的固定地址 |

> **注意**：`vpngate.py` 中**不需要**配置 `EDT_UUID` 和 `EDT_DOMAIN`。你之前看到的这两个变量是旧版遗留，现已删除。edgetunnel 后台会自己处理 UUID 和域名。

### 第 5 步：开启 GitHub Pages 与 Actions

1. 进你 fork 的仓库 → Settings → Pages，Source 设为 **GitHub Actions**
2. 进 Actions 页，若提示启用 Actions 就点启用
3. 手动触发一次：Actions → VPN Gate Node Check → Run workflow → Run workflow
4. 等它跑完（约 1 分钟），看到绿色 ✓ 即成功

### 第 6 步：确认产物

跑完后，你的站点地址是：
~~~text
https://你的GitHub用户名.github.io/仓库名/nodes.txt
~~~
浏览器打开，能看到一堆 `优选域名:443#国家-住宅-XX …` 的行，就说明全部打通了。

---

## 二、使用教程（URL 自动轮换，一次配置永久生效）

1. 进 edgetunnel 后台（你的域名/admin），找到「自定义优选IP」文本框
2. 粘贴**一行网址**：
   ~~~text
   https://你的GitHub用户名.github.io/仓库名/<SECRET_KEY>/nodes.txt
   ~~~
   > `SECRET_KEY` 是你在 Actions Secret 里设置的随机密钥。设置后所有产物只发布在 `/<SECRET_KEY>/` 下，根路径 `nodes.txt` 不再生成；不设置则退回根路径 `nodes.txt`（公开可读）。
3. 点保存
4. 客户端刷新订阅 → 每次刷新 edgetunnel 都重新拉取一次 nodes.txt，节点自动更新

> 原理：`nodes.txt` 是纯节点行版本（无注释头），每行 `入口域名:端口#国家-住宅-01-F0$sstp://vpn:vpn@节点:端口`。名称末尾的 `F<数字>` 是 Scamalytics 欺诈分（0 最干净，100 最脏，越低越靠前）；没有出口 IP 的兜底节点不带后缀。延迟由客户端自己测。edgetunnel 下次生成订阅时会 fetch 这个网址、逐行解析成优选入口 + 链式代理指令。你只填一次，之后节点每小时自动换、零手动。

---

## 三、如何更换优选域名

入口地址用的是「优选域名」，决定客户端连 Cloudflare 用哪个 IP、稳不稳。

### 在哪个文件改
- 文件：`edge_hosts.txt`（一行一个，格式 `域名:端口`，# 开头为注释）

### 改法
1. 用测速工具（如 bestcf）测一批 Cloudflare 优选域名，挑「延迟低 + 实际能连通」的
2. 打开 `edge_hosts.txt`，替换/追加你测出来的域名
3. 提交推送，等下一次自动运行或手动触发 Action

> 不想动代码：设置 GitHub Actions 变量 `EDGE_HOSTS`（整体替换，逗号分隔）或 `EDGE_HOSTS_EXTRA`（在默认池后追加）。默认池已内置 32 个静态优选域名（含 BestCF 聚合站）。

---

## 四、配置速查表（环境变量 / 常量）

### 项目结构

~~~text
vpngate.py       入口: 只做流程编排
config.py        环境变量 + 数据文件 -> Config dataclass (import 时无副作用)
sources.py       VPN Gate 抓取/解析/筛选 SSTP/去重
edge.py          静态优选域名 + 优选 API (并发拉取、校验、去重)
checker.py       Worker 检测、连接池、网络类型判定
output.py        data.json / index.html / nodes.txt 生成
logsetup.py      标准 logging 分区输出
edge_hosts.txt   静态优选域名, 一行一个
optimal_apis.txt 优选 API 默认列表, # section: ct|cu|cmcc|extra 分组
tools/probe_sstp.py  诊断脚本: 验证 OpenVPN remote 端口是否就是 SSTP 端口
~~~

> 端口假设已实测（2026-11-03，GitHub Runner）：抽样 10 个节点，`remote` 端口 10/10 返回 `HTTP/1.1 200 OK`（SSTP 握手成功）；其中 1 个节点仅 `1908` 端口可用、443 超时。因此 `to_sstp_nodes` 继续使用 OpenVPN 的 remote 端口，不改成 443。

> `HOSTS_ENTRY` 是旧版遗留变量：设置后用它的值整体替代 `edge_hosts.txt` 的静态域名（`EDGE_HOSTS_EXTRA` 仍会追加）。新部署建议直接改 `edge_hosts.txt`。

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `EDGE_HOSTS` | `edge_hosts.txt`（32 个） | 整体替换静态域名池（逗号分隔，`域名:端口`） |
| `EDGE_HOSTS_EXTRA` | 空 | 在默认池后追加自定义域名，不覆盖默认值 |
| `HOSTS_ENTRY` | 空 | 旧版入口覆盖（设置后替代 `edge_hosts.txt` 的静态域名） |
| `OPTIMAL_API_CT` | `optimal_apis.txt` 的 `ct` 分组 | 电信线路优选 API（可填多个，逗号分隔；置空即关闭该线路） |
| `OPTIMAL_API_CU` | `optimal_apis.txt` 的 `cu` 分组 | 联通线路优选 API |
| `OPTIMAL_API_CMCC` | `optimal_apis.txt` 的 `cmcc` 分组 | 移动线路优选 API |
| `OPTIMAL_API_EXTRA` | `optimal_apis.txt` 的 `extra` 分组 | 追加任意优选 API（逗号/换行分隔） |
| `OPTIMAL_API` | 空 | 旧版总开关；设置后**完全替代**上面四项（逗号分隔） |
| `OPTIMAL_API_TIMEOUT` | `15` | 单个优选 API 请求超时（秒） |
| `EDGE_PORTS` | `443` | 入口端口池；默认只用 443。想试备用端口可设 `443,2053,8443`（部分网络会封这些端口，导致节点全超时） |
| `CHECK_RETRIES` | `1` | 单节点检测失败后的重试次数（0 = 不重试） |
| `CHECK_SAMPLES` | `1` | Worker 成功后的采样次数（仅写入 `data.json`，不参与命名；1 = 单次） |
| `SSTP_PROBE` | `1` | 对所有节点独立做一次 SSTP 握手验证；Worker 判失败但握手通过的节点予以保留（不参与命名）；`0` 关闭 |
| `SSTP_PROBE_TIMEOUT` | `8` | 兜底 SSTP 握手超时（秒） |
| `PURITY_CHECK` | `1` | Scamalytics 欺诈分换算的纯净度（`purity = 100 - fraud_score`，节点名 `F<分数>`）；同时给出综合质量分 `quality`（0–10：风险 45% + 类型 20% + 延迟 10% + 验证方式 10% + 解锁潜力 15%；解锁能力无法在 CI 实测，用住宅类型作代理值）；`0` 关闭 |
| `PURITY_TIMEOUT` | `10` | 单次纯净度查询超时（秒） |
| `PURITY_MAX_NEW` | `100` | 每轮最多查询的新出口 IP 数；历史 IP 复用上一轮分数 |
| `NODE_BLACKLIST_FAILS` | `5` | 同一节点连续失败达到该次数后，24 小时内跳过检测并清出历史/输出；24h 后移出黑名单；`0` 关闭 |
| `BLACKLIST_URL` | 空 | 外部黑名单 txt（逗号分隔多个 URL），每行 `host:port`，每轮拉取合并 |
| `BLACKLIST_FILE` | 仓库根目录 `blacklist.txt` | 本地黑名单文件；客户端长期失败的节点写这里，下一轮 CI 自动清理 |
| `BLACKLIST_SKIP_HOURS` | `24` | 连续失败拉黑后的跳过时长（小时），过期直接移出黑名单（不重检） |
| `STATS_TTL_DAYS` | `7` | `node_stats` 记录保留天数（长期不出现的节点会被清理） |
| `WORKER_CHECK_URL` | 未设置（必须配置） | 检测 Worker。**只填在本地环境变量 `CHECK_WORKER` 或 Actions Secret `DOMAIN` 里，不要写进代码/仓库** |
| `SECRET_KEY` | 空 | 订阅密钥（16-64 位 `A-Za-z0-9_-`）。设置后产物只发布到 `/<SECRET_KEY>/nodes.txt`，根路径 404。**只放 Actions Secret** |
| `NODES_URL` | 按 `SITE_URL` + `SECRET_KEY` 自动拼接 | 固定订阅地址（fork 后改成你自己的） |
| `PREVIOUS_DATA_URL` | 按 `SITE_URL` + `SECRET_KEY` 自动拼接的 `data.json` | 上一轮产物，用作"历史节点"来源：本轮会把其中的可用节点合并进来一起复检，仍可用就保留，刷新不会丢掉旧节点 |

### 优选 API 支持范围

- **纯文本**：`1.2.3.4`、`1.2.3.4:2053`、`1.2.3.4#备注`、`1.2.3.4,5.6.7.8`
- **JSON**：`{"info":[{"ip":"1.2.3.4","colo":"HKG"}]}` 等结构（自动递归提取 `ip`/`host`/`address` 字段）
- **自动过滤**：HTML 错误页、CIDR、内网/保留地址、超范围端口、非法主机名
- **默认聚合源**（本地实测全部可用）：
  - `cf.090227.xyz`（CT / CU / CMCC 三网）
  - `090227.pages.dev/bestcf`（BestCF 聚合，含域名 + IP，`isp=all|ct|cu|cmcc`，`ips=50`）
  - `addressesapi.090227.xyz`（`ct` / `cmcc` / `CloudFlareYes` / `ip.164746.xyz`）
  - `ipdb.api.030101.xyz`（ymyuuu/IPDB BestCF）
  - `raw.githubusercontent.com/ymyuuu/IPDB/.../bestcfv4.txt`
  - `ip.164746.xyz/ipTop.html`（CloudflareSpeedTest Top）
- 默认运行实测：14 个 API 源 → 54 个端点，静态域名 32 个，合并入口池 85 个。
- `ips=50` 已写入所有默认源；但上游并不都支持该参数：`090227.pages.dev` 固定返回 20 条、`addressesapi` 固定 6 条，`cf.090227.xyz` 由服务端决定条数，所以端点总量仍约 54。想要更多入口，往 `OPTIMAL_API_EXTRA` 追加源即可。
- 想接入需要 key 的接口（如 `https://www.wetest.vip/api/cf2dns/get_cloudflare_ip?key=xxx&type=v4`）或自建接口：填 `OPTIMAL_API_EXTRA` 即可，JSON 会自动解析。

> 再次强调：`vpngate.py` **不需要**配置 `EDT_UUID` 和 `EDT_DOMAIN`，这两个参数属于 edgetunnel 本身。

---

## 五、常见问题

### 只有几个节点能连
入口优选域名大部分被墙。用 bestcf 重新测速，把 `EDGE_HOSTS` 换成实测能通的域名。

### 全部 -1
检查：edgetunnel 是否部署好、域名是否解析到 Cloudflare、UUID 是否填对、传输协议是否对得上。

### 1 小时没更新
到 Actions 页看最近一次运行是否成功、cron 是否还在。

### 检测 Worker 报错
确认 Worker 部署成功、域名填对（Actions Secret `DOMAIN`），浏览器直接访问 `https://你的Worker/check?sstp=...` 看是否返回 JSON。

### 不知道 UUID 和节点域名在哪里看
登录 edgetunnel 后台（`https://你的域名/admin`），在后台首页就能看到。

---

*流水线：GitHub Actions（每小时 cron） → vpngate.py → 检测 Worker → GitHub Pages*

---

## 引用与致谢

本项目的实现离不开以下开源项目和服务的支持，在此表示衷心的感谢：

| 项目 | 用途 | 链接 |
| :--- | :--- | :--- |
| **cmliu/edgetunnel** | VLESS 代理 + 链式代理，节点最终通过它使用 | https://github.com/cmliu/edgetunnel |
| **lsh8848/cm-Workers-CheckSocks5** | 检测 Worker：验证 SSTP 节点可用性并读取出口 IP | https://github.com/lsh8848/cm-Workers-CheckSocks5 |
| **fdciabdul/Vpngate-Scraper-API** | VPN Gate 节点数据的 GitHub 镜像（官方源失效时回退） | https://github.com/fdciabdul/Vpngate-Scraper-API |
| **VPN Gate** | SSTP 节点数据源 | https://www.vpngate.net/ |
| **Star History** | 提供项目热度曲线图生成服务 | https://star-history.com/ |

---

## 项目热度

[![Star History Chart](https://api.star-history.com/svg?repos=hezhanleiok/gate&type=Date)](https://star-history.com/#hezhanleiok/gate&Date)

---

**特别感谢**：
感谢所有为开源社区做出贡献的开发者们！没有你们的无私奉献，就没有这个项目的诞生。也感谢每一位使用、测试和反馈问题的用户，是你们的支持让这个项目不断完善。
