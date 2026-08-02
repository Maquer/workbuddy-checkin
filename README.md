# workbuddy-checkin

腾讯 CodeBuddy (WorkBuddy) 独立签到 / 积分查询脚本。

从 [cpa-plugin/workbuddy](https://github.com/Sliverkiss/cpa-plugin/tree/main/workbuddy) 插件的 `billing.go` + `keepalive.go` 逻辑中提取核心 API 调用，重写为**纯 Python 标准库**实现，零第三方依赖，适合沙箱 / 容器 / cron 部署。

## 功能

- **多账号批量签到** — CN 账号每日签到，自动跳过 Global / 已禁用账号
- **积分查询** — 多套餐包聚合，人类可读日报 + JSON 格式
- **Token 自动刷新** — 过期前 24h 预刷新，refreshToken 轮换落盘
- **会话失效检测** — 12153 错误自动标记 disabled，避免无效请求
- **Trial 领取** — Global 账号一次性 250 积分专家加油包
- **定时模式** — 内置 cron，每天指定时间自动签到
- **凭证兼容** — 同时支持嵌套（插件 OAuth）和扁平（UI 导入）两种 JSON 格式

## 快速开始

### 1. 准备凭证

从 CPA workbuddy 插件或 CPA-Manager-Plus 面板导出凭证文件，放入 `auths/` 目录：

```
auths/
  workbuddy-<uid1>.json
  workbuddy-<uid2>.json
```

凭证文件格式（嵌套，与 CPA 插件兼容）：

```json
{
  "auth": {
    "accessToken": "...",
    "refreshToken": "...",
    "expiresAt": 1234567890,
    "domain": "codebuddy.cn"
  },
  "account": {
    "uid": "...",
    "enterpriseId": "...",
    "nickname": "..."
  },
  "disabled": false
}
```

也支持扁平格式：

```json
{
  "accessToken": "...",
  "refreshToken": "...",
  "expiresAt": 1234567890,
  "domain": "codebuddy.cn",
  "uid": "...",
  "nickname": "..."
}
```

> `domain` 决定区域路由：`codebuddy.cn` → CN（支持签到），`workbuddy.ai` → Global（仅 trial）。

### 2. 运行

```bash
# 批量签到（所有账号）
python3 workbuddy_checkin.py signin

# 积分日报（人类可读）
python3 workbuddy_checkin.py credit

# 积分日报（JSON 格式）
python3 workbuddy_checkin.py credit -j

# 指定账号积分
python3 workbuddy_checkin.py credit <uid>

# 领取 trial（仅 Global 账号）
python3 workbuddy_checkin.py trial <uid>

# 强制刷新所有 token
python3 workbuddy_checkin.py refresh

# 定时模式（每天 9 点签到）
python3 workbuddy_checkin.py cron 9
```

### 3. Docker 部署

```bash
# 准备凭证目录
mkdir -p auths
# 把 workbuddy-*.json 放入 auths/

# 启动（每天 9 点自动签到）
docker compose up -d --build

# 手动签到
docker compose exec workbuddy python3 workbuddy_checkin.py signin

# 查看积分
docker compose exec workbuddy python3 workbuddy_checkin.py credit
```

### 4. 系统 cron 部署

```bash
# 每天早上 9 点签到
0 9 * * * cd /path/to/workbuddy-checkin && python3 workbuddy_checkin.py signin >> /var/log/workbuddy-checkin.log 2>&1
```

## 命令一览

| 命令 | 说明 |
|---|---|
| `signin [auths_dir]` | 批量签到所有 CN 账号 |
| `credit [uid] [auths_dir] [-j]` | 积分查询（`-j` 输出 JSON） |
| `trial <uid> [auths_dir]` | 领取 Global trial 加油包 |
| `refresh [auths_dir]` | 强制刷新所有 token |
| `cron <hour> [auths_dir]` | 定时模式（每天 hour 点签到） |

## API 端点映射

脚本核心逻辑来自 CPA workbuddy 插件，端点对照：

| 功能 | 端点 | 来源 |
|---|---|---|
| 签到状态 | `POST /v2/billing/meter/checkin-activity-status` | `billing.go fetchCheckinStatus` |
| 执行签到 | `POST /v2/billing/meter/daily-checkin` | `billing.go performCheckinCall` |
| 积分查询 | `POST /v2/billing/meter/get-user-resource` | `billing.go fetchUserResource` |
| Token 刷新 | `POST /v2/plugin/auth/token/refresh` | `keepalive.go refreshCall` |
| Trial 领取 | `POST /billing/ide/trial` | `billing.go performTrialCall` |

区域路由（`billing.go billingBaseFor`）：
- CN: `https://www.codebuddy.cn`
- Global: `https://www.workbuddy.ai`

## 生命周期

| 状态 | CN 账号 | Global 账号 |
|---|---|---|
| 正常 | 支持每日签到 | 不支持签到 |
| Token 过期 | 自动刷新 | 自动刷新 |
| 会话失效 (12153) | 标记 disabled，需重新登录 | 标记 disabled，需重新登录 |
| Trial | 不适用 | 每账号一次，250 积分 |

## 脱敏

- 凭证文件 `auths/` 已 gitignore，不会上传
- 输出日志只显示 UID / Nickname / 积分，不打印 token
- Token 刷新后原子写回（临时文件 + rename）

## 技术栈

- Python 3.8+（纯标准库，零依赖）
- `urllib.request` — HTTP 请求
- `json` — 凭证解析 / API 信封
- `argparse` — 命令行
- `base64` — JWT 区域判断

## License

MIT
