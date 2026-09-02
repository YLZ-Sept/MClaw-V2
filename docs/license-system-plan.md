# 离线授权（License）系统 — 实施方案

> 状态：已实施 / 2026-09-02（实施结果见文末「实施记录」）
> 决策前提：**装完即锁**（无试用期）、**不分档位**（每单自定义 feat + users）、**仅 Windows 桌面安装包**、**到期前告警 + 7 天宽限期**

---

## 1. 核心思路

私钥签发、公钥验签的非对称体系。客户拿到全部源码和公钥也造不出合法授权码——安全性建立在 Ed25519 的数学性质上，而非代码保密。

```
你（签发端）                      客户机器（验证端）
─────────────                    ─────────────
持有：Ed25519 私钥（仓库外）        持有：公钥（编译进代码，公开无妨）
工具：tools/license_gen.py         代码：src/mclaw/license/
```

### 1.1 为什么用 Ed25519 而非 RSA-PSS

签名 64 字节 vs RSA-2048 的 256 字节。授权码总长从 ~400 字符降到 ~180，客户复制粘贴不易断行出错。验签实测 **48.6 µs**（本机 cryptography 49.0.0），每请求验签的开销可忽略。

---

## 2. 硬件指纹（关键修正点）

### 2.1 原方案的缺陷

参考文档写的是「三项硬件信息**拼接后** SHA-256，取前 20 位切成 5 段」。这是无效的：哈希的雪崩效应决定了任何一个部件变化都会导致整个摘要全变，**5 段一段都对不上**。所谓「换硬盘不会锁死正版客户」在该实现下根本不成立，实际行为是任何硬件变动立即锁死。

### 2.2 正确实现：每部件独立哈希

```
主板序列号   → SHA256(SALT + "board:" + 值)[:2字节] → 段1
BIOS 序列号  → SHA256(SALT + "bios:"  + 值)[:2字节] → 段2
系统盘序列号 → SHA256(SALT + "disk:"  + 值)[:2字节] → 段3
物理网卡 MAC → SHA256(SALT + "mac:"   + 值)[:2字节] → 段4
MachineGuid  → SHA256(SALT + "guid:"  + 值)[:2字节] → 段5

指纹 = "2F72-4463-9B50-7666-617F"
```

换一块硬盘只废掉段 3，仍有 4/5 匹配，容错才真正成立。

### 2.3 本机实测的五个数据源

| 段 | 来源 | 本机实测值 |
|---|---|---|
| 1 | `Win32_BaseBoard.SerialNumber` | `EGW58M103XN` |
| 2 | `Win32_BIOS.SerialNumber` | `T8NRKD01C73134C` |
| 3 | `Win32_DiskDrive` (Index=0) `.SerialNumber` | `0025_3847_51B3_2E72.` |
| 4 | `Win32_NetworkAdapter` 首个物理网卡 `.MACAddress` | `A0:AD:9F:9B:D6:9E` |
| 5 | `HKLM\SOFTWARE\Microsoft\Cryptography` → `MachineGuid` | `2a71db46-...` |

**已剔除 `Win32_Processor.ProcessorId`**：实测返回 `BFEBFBFF000C0662`，这是 CPUID 特征位 + 型号编码，**同型号 CPU 的所有机器完全相同**，几乎不提供熵，不能作为指纹源。参考文档里的「CPU/主板 UUID」若指 ProcessorId 是错的。

### 2.4 缺失段与匹配规则

- 某部件取不到 → 该段记为 `----`
- **`----` 段永不计入匹配**（否则两台都缺盘序列号的机器会在该段互相匹配，这是原方案的隐性漏洞）
- 匹配阈值：5 段中 **≥3 段相同**
- **激活时**若可用段 < 3，直接拒绝激活并提示——避免留一个必然失效的授权给客户

### 2.5 性能：必须缓存

实测 PowerShell 采集耗时 **1.25 秒**。绝不能每请求执行，否则接口直接废掉。

策略：**进程启动时采集一次，缓存在内存**（硬件不会热变）。中间件每请求只做验签 + 比日期，都是纯内存操作。

### 2.6 采集实现方式

单次 PowerShell 调用取回全部 5 项（避免 5 次进程启动）。使用 `subprocess.run` 加 `timeout=30`、`CREATE_NO_WINDOW` 标志（防止桌面端弹黑框）。任何一项失败不影响其他项。

---

## 3. 授权码格式

```
MC1.<base64url(payload_json)>.<base64url(ed25519_sig)>
```

签名覆盖 `"MC1." + base64url(payload)` 的 UTF-8 字节。payload 为明文可解码（客户可自行查看授权内容，这是刻意设计——透明度换信任）。

### 3.1 payload 字段

```json
{
  "v": 1,
  "sn": "MC-2026-0001",
  "cust": "某某科技有限公司",
  "fp": "2F72-4463-9B50-7666-617F",
  "iss": "2026-09-02",
  "exp": "2027-03-02",
  "tier": "自定义备注",
  "users": 20,
  "feat": ["plugins", "skills", "mcp", "knowledge_base", "im_channels"]
}
```

- `sn` — 你的台账编号，客户报障时报此号即可查签发记录
- `feat` — **显式列出**而非从 tier 推导。以后调整功能划分不用重签老客户的码
- `users` — `0` 表示不限
- `tier` — 因决定「不分档」，此字段仅作自由文本备注，不参与任何逻辑判断

---

## 4. 中间件与请求链路

### 4.1 注册顺序

FastAPI/Starlette 中间件是 LIFO——**后注册的先执行**。目标执行顺序：

```
CORS → setup gate(428) → auth(401) → license gate(402) → 业务路由
```

因此在 `src/mclaw/api/server.py` 的注册顺序为（license gate **最先注册**）：

```python
license_gate_mw = create_license_gate_middleware(license_manager)  # 新增，最先
app.middleware("http")(license_gate_mw)

auth_mw = create_auth_middleware(web_access_config)                # server.py:756 现有
app.middleware("http")(auth_mw)

setup_gate_mw = create_setup_gate_middleware(web_access_config)    # server.py:764 现有
app.middleware("http")(setup_gate_mw)

app.add_middleware(CORSMiddleware, **cors_kwargs)                  # server.py:788 现有
```

### 4.2 为什么 license gate 在 auth 之后

激活接口必须要求 admin 已登录。若排在 auth 之前，局域网内任何人都能覆盖客户的授权码、或读取机器指纹。放在 auth 之后时 `request.state.user_id` 已就绪，可直接做 admin 校验。

**连带影响（因选择「装完即锁」）**：客户装完必须先完成 setup 设密码 → 登录 → 才能看到激活页拿指纹。这是「无试用」的必然代价，需在交付文档里向销售说明。

### 4.3 白名单

license gate 放行（但**不**在 auth 白名单里，必须已登录）：

```
/api/license/status        # 前端探测授权状态
/api/license/fingerprint   # 读取本机指纹给客户发给你
/api/license/activate      # 提交授权码（admin only）
```

license gate 放行（沿用 setup gate 的清单）：`/`、`/api/health`、`/api/healthz`、`/api/readyz`、`/api/auth/*`、`/api/logs/frontend`、`/web/`、`/docs`、`/redoc`、`/openapi.json`、`/user-docs`、`/static/`

### 4.4 每请求校验流程

```
请求 → 非 /api/* ? → 放行
     → 在白名单 ? → 放行
     → 读内存中的已验证 license 状态
        ├─ 无授权     → 402 {"error":"license_required"}
        ├─ 验签失败   → 402 {"error":"license_invalid"}   + 落盘标记 revoked
        ├─ 指纹失配   → 402 {"error":"license_mismatch"}
        ├─ 超宽限期   → 402 {"error":"license_expired", "expired_days":N}
        └─ 通过       → request.state.license = payload（业务可读 feat/users）
```

**重要**：验签结果在启动时算一次并缓存，但**不是简单信任数据库里的 active 标记**——每次进程启动都重新完整验签 + 重新比对指纹。运行期改 `license.json` 不会生效（下次启动才读），改数据库标记也无用。这保留了原方案「防绕过」的核心性质，同时避免每请求 1.25 秒的指纹采集。

---

## 5. 时钟回拨防护

原方案承认「改系统时间可延长有效期」是已知弱项。补法：**单调水位线**。

- `data/license.json` 中记录 `last_seen_utc`
- 每小时节流更新一次（不是每请求写盘）
- 校验时若 `now < last_seen_utc - 24h`（24h 容差覆盖时区/NTP 校正）→ 判定为篡改，进入 `license_invalid`

约 40 行代码，堵住最常见的绕过手法。

---

## 6. 功能开关落点（已勘定）

| 开关 | 卡点 file:line |
|---|---|
| `plugins` | `src/mclaw/plugins/manager.py:496` 加载循环 |
| `skills` | `src/mclaw/skills/registry.py:956` `get_tool_schemas`（LLM 可见工具表）+ `:918` `find_relevant` |
| `mcp` | `src/mclaw/tools/mcp.py:360` `MCPClient.connect` 早返回（已有同款早返回模式在 `:380`） |
| `im_channels` | `src/mclaw/main.py:177` `_create_bot_adapter` 返回 `None`（调用方 `:296` 已处理 None） |
| `knowledge_base` | `src/mclaw/api/server.py:1113` 跳过 `include_router` |

### 6.1 最大用户数

卡在 `src/mclaw/api/auth.py:261` `add_user()` 开头（用户名校验之后、哈希之前）：

```python
limit = get_license_user_limit()   # 0 = 不限
if limit and len(self._users) >= limit:
    raise ValueError(f"授权用户数上限为 {limit}，请联系供应商升级")
```

放在 `add_user` 而非路由层——调用方 `routes/auth.py:460` 已将 `ValueError` 映射为 HTTP 409，且未来新增调用方自动受控。

### 6.2 已知限制：改档位需重启

插件、技能、IM 通道都在**启动时**加载。变更 `feat` 后需重启后端才生效。激活页给「部分功能需重启生效」提示即可——为此改造三个加载器不划算。

---

## 7. 前端

完全复刻现有 setup gate 的链路：

| 环节 | 现有实现（参照） | 新增 |
|---|---|---|
| 全局拦截 | `providers.ts:326` `safeFetchResponse` 中 428 分支 | 加 402 分支，派发 `mclaw:license-required` |
| 状态位 | `App.tsx:294` `setupRequired` | `licenseRequired` |
| 启动探测 | `App.tsx:323` 探 `/api/auth/setup-status` | 探 `/api/license/status` |
| 渲染门禁 | `App.tsx:4964` `if (setupRequired) return <SetupView/>` | 在 LoginView **之后**加 `if (licenseRequired) return <LicenseView/>` |
| 页面 | `views/SetupView.tsx` | `views/LicenseView.tsx` |
| i18n | `setup.*` keys | `license.*` keys（zh/en） |

**门禁顺序**：setup → login → license。因为激活需要已登录身份。

`LicenseView` 内容：显示本机指纹（带一键复制）、授权码粘贴框、激活按钮、失败原因提示、联系方式。

### 7.1 到期告警条

已激活但临近到期时不拦截，只在主界面顶部显示横幅：
- 到期前 ≤30 天：黄条「授权将于 N 天后到期」
- 已到期、宽限期内（≤7 天）：红条「授权已到期，剩余宽限 N 天」

---

## 8. 签发端

`tools/license_gen.py` —— CLI 工具，**不打进 wheel**（在 `pyproject.toml` 的打包配置中排除）。

```bash
python tools/license_gen.py \
  --key   E:/mclaw-license-keys/private.pem \
  --cust  "某某科技有限公司" \
  --sn    MC-2026-0001 \
  --fp    2F72-4463-9B50-7666-617F \
  --months 6 \
  --users 20 \
  --feat  plugins,skills,mcp,knowledge_base,im_channels
```

同时追加一行到台账 CSV（`sn, cust, fp, iss, exp, users, feat`），方便对账和续费提醒。

### 8.1 密钥管理（硬红线）

- 私钥**绝不进仓库**，放仓库外目录（如 `E:\mclaw-license-keys\`）并另做离线备份
- `.gitignore` 追加 `*.pem`、`*license*key*` 作为兜底
- 公钥以字节常量硬编码在 `src/mclaw/license/keys.py`
- **私钥一旦泄露整套体系归零**，且无法远程吊销已签发的码

---

## 9. 依赖调整（含一个既存冲突）

`cryptography` 目前是**可选依赖**，需提升为核心依赖才能进安装包 wheelhouse：

- `pyproject.toml:104` — `wework_ws` extra: `cryptography>=46.0.7`
- `pyproject.toml:177` — `all` extra: `cryptography>=46.0.7`
- `pyproject.toml:199` — **`finance-auto` extra: `cryptography>=42.0,<46.0`** ← 与上面两处直接冲突

这是**既存**的依赖冲突（同时装 `[all]` 和 `[finance-auto]` 会解不出来），提升为核心依赖时会暴露。

处理：核心依赖写 `cryptography>=42.0`（Ed25519 自 2.6 起支持，42.0 远超需求），并把 finance-auto 的上界 `<46.0` 放宽——需先确认该插件用的 AES-256-GCM / PBKDF2 API 在 46+ 未变更。若确认有破坏性变更，则保留上界并在核心依赖同步收窄。

本机实测已装 `cryptography 49.0.0`，开发环境无阻碍。

---

## 10. 文件清单

**新增**

```
src/mclaw/license/__init__.py
src/mclaw/license/keys.py          # 公钥常量
src/mclaw/license/fingerprint.py   # 硬件指纹采集 + 匹配（含启动缓存）
src/mclaw/license/verifier.py      # Ed25519 验签 + payload 解析
src/mclaw/license/manager.py       # 状态机、license.json 读写、时钟水位线
src/mclaw/api/middleware_license_gate.py
src/mclaw/api/routes/license.py    # status / fingerprint / activate
tools/license_gen.py               # 签发 CLI（不打包）
apps/setup-center/src/views/LicenseView.tsx
tests/test_license_verifier.py
tests/test_license_fingerprint.py
tests/test_license_gate.py
```

**修改**

```
src/mclaw/api/server.py            # 注册中间件 + 路由 + KB 开关(:1113)
src/mclaw/api/auth.py:261          # add_user 用户数上限
src/mclaw/plugins/manager.py:496   # plugins 开关
src/mclaw/skills/registry.py:918,956  # skills 开关
src/mclaw/tools/mcp.py:360         # mcp 开关
src/mclaw/main.py:177              # im_channels 开关
pyproject.toml                     # cryptography 转核心依赖 + 冲突处理
requirements.txt                   # 同步
apps/setup-center/src/App.tsx      # licenseRequired 状态 + 门禁 + 告警条
apps/setup-center/src/providers.ts # 402 拦截
apps/setup-center/src/i18n/{zh,en}.json
.gitignore                         # *.pem 兜底
```

---

## 11. 实施顺序

1. **密钥与核心库** — 生成密钥对、`license/` 四个模块、`license_gen.py`、单测
2. **指纹实机验证** — 在本机和虚拟机 `192.168.0.17` 上跑，确认取值稳定、容错符合预期（**此步是后续所有工作的前提，不过不往下走**）
3. **中间件与路由** — gate + `/api/license/*` + 单测
4. **功能开关** — 五个卡点 + 用户数上限
5. **前端** — LicenseView + 402 拦截 + 门禁 + 告警条 + i18n
6. **打包** — 依赖调整、确认 wheelhouse 含 cryptography、排除 `tools/`
7. **端到端** — 虚拟机全新安装 → setup → 登录 → 激活 → 功能开关 → 到期/宽限/篡改各分支

---

## 12. 必须坦白的上限

后端以 wheel 形式装在客户机器的 `app-venv`，`.py` 源码明文可读。**技术型客户删掉 gate 代码即可绕过**——这是所有本地授权的共同天花板，参考文档也承认。可用 pyc-only 分发抬高门槛，但挡不住真想破解的人。

**商业上靠 3~6 个月短期授权滚动续费控制风险，比在技术上死磕划算。**

另需明确：**纯离线、无远程吊销**。授权码一旦签发，在到期前无法收回（客户内网部署本就连不上你的服务器）。签发时的期限选择就是唯一的风险闸门。

---

# 实施记录（2026-09-02）

全部 6 项任务已完成，**新增 103 项单测全部通过**，后端回归 798 passed（与实施前基线一致）。

## 与原计划的三处偏差

**1. `MISSING_SEGMENT` 由 `----` 改为 `XXXX`**

单测抓到的真 bug：`----` 与段分隔符 `-` 相同，`split("-")` 会把占位段炸成 5 个空串，容错逻辑直接失效。四位十六进制段只含 `0-9A-F`，`XXXX` 永不与真实段位碰撞。

**2. 剔除 `Win32_Processor.ProcessorId`，改用主板+BIOS 序列号**

实测该字段返回 `BFEBFBFF000C0662`，是 CPUID 特征位+型号编码，**同型号 CPU 的所有机器完全相同**，几乎不提供熵。参考文档里的「CPU/主板 UUID」若指此字段则是错的。

**3. 新增测试环境开关 `MCLAW_DISABLE_LICENSE`**

`tests/conftest.py` 中默认置 `1`。否则每个走 `create_app()` 的测试都会进入未授权态、插件/技能全关，破坏大量既有测试。授权自身的测试直接构造 `LicenseManager`，不受此开关影响（已验证）。

## 端到端验证结果

用**真实生产私钥**为**本机真实指纹**签码，跑通完整客户旅程：

| 阶段 | 结果 |
|---|---|
| 全新安装 | `/api/*` → 428 setup_required |
| 完成设密码 + 登录 | 200 |
| 已登录但未激活 | `/api/skills` → **402 license_required** |
| 读取机器码 | 200，`007B-5B05-11AC-8CE8-C6EB`，5/5 段可用 |
| 提交授权码 | 200 active，`restart_required: true` |
| 激活后 | `/api/skills` → **200** |
| 用户数上限（授权 3 人） | admin/bob/carol 通过，dave → **409** |
| 篡改授权码 | 400 拒绝，**原有效授权未被覆盖** |
| 未授权 knowledge_base | `/api/knowledge/*` → 404（路由根本未挂载） |

中间件顺序经实机确认为 `CORS → setup_gate → auth → license_gate`，与设计一致。

指纹容错也已验证：换硬盘（1 段变化）4/5 通过；换 3 个部件 2/5 拒绝；缺失段不计入匹配。

## 依赖冲突已解决

`pyproject.toml:199` 的 `cryptography>=42.0,<46.0` 与核心/`wework_ws` 的 `>=46.0.7` 原本直接冲突（同时安装 `[all]` 和 `[finance-auto]` 解不出来）。已移除上界——finance-auto 用到的 `AESGCM`/`HKDF`/`PBKDF2HMAC` 在 50.0.0 上实测全部可用。安装包 wheelhouse 已含 `cryptography-50.0.1-cp311-abi3-win_amd64.whl`。

## 密钥位置

- 私钥：`E:\mclaw-license-keys\private.pem`（**仓库外**，需离线备份）
- 台账：`E:\mclaw-license-keys\license_ledger.csv`（签发时自动追加）
- 公钥：`src/mclaw/license/keys.py`，`94fc31b3...`（key_id `2026-09`）

`.gitignore` 已加 `*.pem` / `*private*key*` / `license_ledger.csv` 作为兜底。

## 尚未验证

- **虚拟机实机安装**：本机 5/5 指纹可用，但换台机器（尤其虚拟机）各部件可用性未知。建议在 `192.168.0.17` 上跑一次 `/api/license/fingerprint` 确认段数 ≥3。
- **完整安装包打包**：`build_full.ps1` 未跑，wheelhouse 里 cryptography 是既有的（此前作为传递依赖被拉入），提为核心依赖后应无变化，但未实测。
