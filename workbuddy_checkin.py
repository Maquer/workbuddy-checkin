#!/usr/bin/env python3
"""
workbuddy-checkin — 腾讯 CodeBuddy (WorkBuddy) 独立签到 / 积分查询脚本。

从 cpa-plugin/workbuddy 插件的 billing.go + keepalive.go 逻辑中提取核心 API
调用，重写为纯 Python 标准库实现，零第三方依赖，适合沙箱 / 容器 / cron 部署。

功能:
  - 多账号批量签到（CN 账号）
  - 积分查询（人类可读 + JSON）
  - Token 自动刷新（过期前 24h 预刷新，refreshToken 轮换落盘）
  - Global 账号领取一次性 trial 专家加油包
  - 单次执行 / cron 定时模式

用法:
  python3 workbuddy_checkin.py signin [auths_dir]       # 批量签到
  python3 workbuddy_checkin.py credit [auths_dir]        # 积分日报
  python3 workbuddy_checkin.py credit <uid> [auths_dir]  # 指定账号积分
  python3 workbuddy_checkin.py trial <uid> [auths_dir]   # 领取 trial（Global）
  python3 workbuddy_checkin.py refresh [auths_dir]       # 强制刷新所有 token
  python3 workbuddy_checkin.py cron <hour> [auths_dir]   # 定时模式（每天 hour 点签到）

凭证文件格式（与 CPA workbuddy 插件兼容，放 auths/ 目录下）:
  workbuddy-<uid>.json:
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
"""

import json
import os
import sys
import time
import glob
import base64
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ─── 上游常量（来自 cpa-plugin/workbuddy/main.go + billing.go）───────────────

# CN / Global 两套 billing 基址
BILLING_BASE_CN = "https://www.codebuddy.cn"
BILLING_BASE_GLOBAL = "https://www.workbuddy.ai"

# Token 刷新端点（两个域名各有一套，按 domain 路由）
TOKEN_REFRESH_CN = "https://copilot.tencent.com/v2/plugin/auth/token/refresh"
TOKEN_REFRESH_GLOBAL = "https://www.workbuddy.ai/v2/plugin/auth/token/refresh"

# 模拟客户端版本（与 CPA 插件保持一致）
CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2"

# Billing API 端点（来自 billing.go）
EP_CHECKIN_STATUS = "/v2/billing/meter/checkin-activity-status"
EP_CHECKIN_STATUS_FALLBACK = "/v2/billing/meter/checkin-status"
EP_DAILY_CHECKIN = "/v2/billing/meter/daily-checkin"
EP_USER_RESOURCE = "/v2/billing/meter/get-user-resource"
EP_TRIAL_CLAIM = "/billing/ide/trial"

# 会话失效标记（来自 keepalive.go sessionDeadMarkers）
SESSION_DEAD_MARKERS = ["Offline user session not found", "12153"]

# Token 过期预刷新余量
TOKEN_REFRESH_MARGIN = 24 * 3600  # 过期前 24h 刷新

# HTTP 超时
HTTP_TIMEOUT = 30

# Bark 推送通知（通过环境变量 BARK_URL 配置）
BARK_URL = os.environ.get("BARK_URL", "")


# ─── 凭证文件读写 ────────────────────────────────────────────────────────────

def load_auth(path):
    """读取凭证 JSON，兼容嵌套（插件格式）和扁平（UI 格式）两种形状。"""
    with open(path, "r") as f:
        raw = f.read()
    data = json.loads(raw)

    # 嵌套形状: {"auth": {...}, "account": {...}}
    if isinstance(data.get("auth"), dict) and data["auth"].get("accessToken"):
        return data

    # 扁平形状: {"accessToken": "...", ...}
    if data.get("accessToken"):
        return {
            "auth": {
                "accessToken": data.get("accessToken", ""),
                "refreshToken": data.get("refreshToken", ""),
                "expiresAt": data.get("expiresAt", 0),
                "domain": data.get("domain", ""),
            },
            "account": {
                "uid": data.get("uid", ""),
                "enterpriseId": data.get("enterpriseId", ""),
                "nickname": data.get("nickname", ""),
            },
            "disabled": data.get("disabled", False),
        }

    raise ValueError(f"凭证文件 {path} 缺少 accessToken")


def save_auth(path, auth_data):
    """原子写回凭证文件（先写临时文件再 rename）。"""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(auth_data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def list_auth_files(auth_dir):
    """遍历目录下所有 workbuddy-*.json 和 workbuddy.json 文件。"""
    pattern1 = os.path.join(auth_dir, "workbuddy-*.json")
    pattern2 = os.path.join(auth_dir, "workbuddy.json")
    files = set(glob.glob(pattern1))
    if os.path.exists(pattern2):
        files.add(pattern2)
    return sorted(files)


# ─── 区域判断 ────────────────────────────────────────────────────────────────

def is_global(auth_data):
    """判断是否为 Global 账号（domain 含 workbuddy.ai）。"""
    domain = (auth_data.get("auth", {}).get("domain") or "").lower()
    return domain == "workbuddy.ai" or domain.endswith(".workbuddy.ai")


def billing_base(auth_data):
    """根据 domain 返回 billing API 基址。"""
    return BILLING_BASE_GLOBAL if is_global(auth_data) else BILLING_BASE_CN


def token_refresh_url(auth_data):
    """根据 domain 返回 token 刷新端点。"""
    return TOKEN_REFRESH_GLOBAL if is_global(auth_data) else TOKEN_REFRESH_CN


# ─── HTTP 工具 ────────────────────────────────────────────────────────────────

def http_post_json(url, body=None, headers=None, timeout=HTTP_TIMEOUT):
    """POST JSON，返回 (parsed_data-or-None, status_code, error-or-None)。

    解析上游 {code, msg, data} 信封: code != 0 视为业务错误。
    """
    if body is None:
        body = {}
    data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", CLIENT_UA)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        # 尝试从响应体提取业务错误码
        try:
            env = json.loads(raw)
            if isinstance(env, dict) and env.get("code", 0) != 0:
                return None, e.code, f"code={env.get('code')} msg={env.get('msg', '')}"
        except (json.JSONDecodeError, ValueError):
            pass
        return None, e.code, f"HTTP {e.code}: {raw[:200]}"
    except urllib.error.URLError as e:
        return None, 0, f"网络错误: {e.reason}"

    # 解析 {code, msg, data} 信封
    try:
        env = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None, status, f"JSON 解析失败: {raw[:200]}"

    if not isinstance(env, dict):
        return env, status, None

    code = env.get("code", 0)
    if code != 0:
        return None, status, f"code={code} msg={env.get('msg', '')}"

    return env.get("data"), status, None


def billing_headers(auth_data):
    """构造 billing API 请求头（来自 billing.go billingHeaders）。"""
    auth = auth_data.get("auth", {})
    acct = auth_data.get("account", {})
    headers = {
        "Authorization": f"Bearer {auth.get('accessToken', '')}",
    }
    if acct.get("uid"):
        headers["X-User-Id"] = acct["uid"]
    if acct.get("enterpriseId"):
        headers["X-Enterprise-Id"] = acct["enterpriseId"]
        headers["X-Tenant-Id"] = acct["enterpriseId"]
    if auth.get("domain"):
        headers["X-Domain"] = auth["domain"]
    return headers


# ─── Token 刷新（来自 keepalive.go refreshCall）──────────────────────────────

def is_token_expired(auth_data, margin=TOKEN_REFRESH_MARGIN):
    """检查 token 是否已过期或即将过期。"""
    expires_at = auth_data.get("auth", {}).get("expiresAt", 0)
    if expires_at == 0:
        return False  # 未知过期时间，不主动刷新
    return time.time() + margin > expires_at


def is_session_dead(error_msg):
    """检查错误是否表示会话已失效（来自 keepalive.go sessionDeadMarkers）。"""
    for marker in SESSION_DEAD_MARKERS:
        if marker in error_msg:
            return True
    return False


def refresh_token(path, auth_data):
    """刷新 access token 并落盘。返回 (success, message)。"""
    refresh_token_val = auth_data.get("auth", {}).get("refreshToken", "")
    if not refresh_token_val:
        return False, "无 refreshToken，无法刷新"

    url = token_refresh_url(auth_data)
    acct = auth_data.get("account", {})
    headers = {
        "X-Refresh-Token": refresh_token_val,
        "X-Auth-Refresh-Source": "workbuddy",
    }
    if acct.get("enterpriseId"):
        headers["X-Enterprise-Id"] = acct["enterpriseId"]

    # token/refresh 不需要 body，但需要正确的 header
    data, status, err = http_post_json(url, body={}, headers=headers)

    if err:
        if status == 401 and is_session_dead(err):
            auth_data["disabled"] = True
            auth_data["note"] = "Session dead (12153): re-login required"
            save_auth(path, auth_data)
            return False, "会话已失效 (12153)，已标记 disabled，需重新登录"
        return False, f"刷新失败 (HTTP {status}): {err}"

    if not data or not data.get("accessToken"):
        return False, "刷新响应中缺少 accessToken"

    # 更新凭证
    auth_data["auth"]["accessToken"] = data["accessToken"]
    if data.get("refreshToken"):
        auth_data["auth"]["refreshToken"] = data["refreshToken"]
    if data.get("domain"):
        auth_data["auth"]["domain"] = data["domain"]

    expires_in = data.get("expiresIn", 0)
    if expires_in > 0:
        auth_data["auth"]["expiresAt"] = int(time.time()) + expires_in

    save_auth(path, auth_data)
    return True, "Token 已刷新"


def ensure_token_valid(path, auth_data):
    """如果 token 即将过期则自动刷新。返回更新后的 auth_data。"""
    if is_token_expired(auth_data):
        print(f"  [刷新] Token 即将过期，自动刷新...")
        ok, msg = refresh_token(path, auth_data)
        if ok:
            print(f"  [刷新] {msg}")
        else:
            print(f"  [刷新] {msg}")
        return auth_data
    return auth_data


# ─── 签到 API（来自 billing.go fetchCheckinStatus / performCheckinCall）──────

def fetch_checkin_status(auth_data):
    """查询签到状态。返回 (status_dict, error)。"""
    base = billing_base(auth_data)
    headers = billing_headers(auth_data)

    # 先尝试主端点，失败再尝试 fallback（billing.go 逻辑）
    for endpoint in [EP_CHECKIN_STATUS, EP_CHECKIN_STATUS_FALLBACK]:
        data, status, err = http_post_json(base + endpoint, body={}, headers=headers)
        if err is None and data is not None:
            return normalize_checkin_status(data), None
        if status == 401:
            return None, f"鉴权失败 (401): {err}"

    return None, err or "签到状态查询失败"


def normalize_checkin_status(data):
    """归一化签到状态字段（兼容 snake_case 和 camelCase）。"""
    if not isinstance(data, dict):
        return {}
    return {
        "active": _bool(data, "active", "Active"),
        "today_checked_in": _bool(data, "today_checked_in", "todayCheckedIn"),
        "streak_days": _int(data, "streak_days", "streakDays"),
        "daily_credit": _int(data, "daily_credit", "dailyCredit"),
        "today_credit": _int(data, "today_credit", "todayCredit"),
        "total_credits": _int(data, "total_credits", "totalCredits"),
        "week_checkin_days": _int(data, "week_checkin_days", "weekCheckinDays"),
        "activity_name": _str(data, "activity_name", "activityName"),
    }


def perform_checkin(auth_data):
    """执行每日签到。返回 (result_dict, error)。"""
    base = billing_base(auth_data)
    headers = billing_headers(auth_data)
    data, status, err = http_post_json(base + EP_DAILY_CHECKIN, body={}, headers=headers)

    if err:
        # 业务错误如"已签到"需要判断
        if status == 401:
            return None, f"鉴权失败 (401): {err}"
        return {"success": False, "message": err}, None

    if not isinstance(data, dict):
        data = {}
    data["success"] = True
    return data, None


def checkin_one_account(path, auth_data):
    """对单个账号执行签到流程。返回结果字典。"""
    acct = auth_data.get("account", {})
    nickname = acct.get("nickname", "")
    uid = acct.get("uid", "")
    result = {
        "uid": uid,
        "nickname": nickname,
        "region": "global" if is_global(auth_data) else "cn",
    }

    # Global 账号不支持签到
    if is_global(auth_data):
        result["success"] = False
        result["skipped"] = True
        result["reason"] = "global"
        result["message"] = "国际版账号不支持签到，可领取一次性 trial 加油包"
        return result

    # disabled 账号跳过
    if auth_data.get("disabled"):
        result["success"] = False
        result["skipped"] = True
        result["reason"] = "disabled"
        result["message"] = "账号已禁用（会话失效）"
        return result

    # 确保 token 有效
    auth_data = ensure_token_valid(path, auth_data)

    # 查询签到状态
    ci_status, ci_err = fetch_checkin_status(auth_data)
    if ci_err:
        result["error"] = ci_err
        result["success"] = False
        # 如果是会话失效，标记 disabled
        if "401" in ci_err and is_session_dead(ci_err):
            auth_data["disabled"] = True
            auth_data["note"] = "Session dead: re-login required"
            save_auth(path, auth_data)
        return result

    result["checkin_status"] = ci_status

    # 已签到则跳过
    if ci_status.get("today_checked_in"):
        result["success"] = True
        result["skipped"] = True
        result["reason"] = "already"
        result["message"] = "今日已签到"
        return result

    # 活动未开启
    if not ci_status.get("active"):
        result["success"] = False
        result["skipped"] = True
        result["reason"] = "inactive"
        result["message"] = "签到活动未开启"
        return result

    # 执行签到
    checkin_result, checkin_err = perform_checkin(auth_data)
    if checkin_err:
        result["error"] = checkin_err
        result["success"] = False
        return result

    # 业务软失败（"已签到"类消息）
    if not checkin_result.get("success", True):
        msg = checkin_result.get("message", "")
        msg_low = msg.lower()
        if "already" in msg_low or "已签" in msg or "今日" in msg:
            result["success"] = True
            result["skipped"] = True
            result["reason"] = "already"
            result["message"] = "今日已签到（上游确认）"
            return result

    result.update(checkin_result)

    # 签到后刷新积分信息
    credits, _ = fetch_user_resource(auth_data)
    if credits:
        result["credits"] = credits.get("total_remain", 0)

    return result


# ─── 积分查询（来自 billing.go fetchUserResource）─────────────────────────────

def fetch_user_resource(auth_data):
    """查询积分（多套餐包聚合）。返回 (credits_summary, error)。"""
    base = billing_base(auth_data)
    headers = billing_headers(auth_data)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    far_future = "2126-12-31 23:59:59"
    body = {
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": now,
        "PackageEndTimeRangeEnd": far_future,
    }

    data, status, err = http_post_json(base + EP_USER_RESOURCE, body=body, headers=headers)
    if err:
        if status == 401:
            return None, f"鉴权失败 (401): {err}"
        return None, err

    # 解析嵌套结构: data.Response.Data.Accounts[]
    resp = data
    if isinstance(data, dict) and "Response" in data:
        resp = data.get("Response", {})
    resp_data = resp.get("Data", {}) if isinstance(resp, dict) else {}

    accounts = resp_data.get("Accounts", [])
    if not isinstance(accounts, list):
        accounts = []

    total_remain = 0
    total_used = 0
    total_size = 0
    packages = []

    for pkg in accounts:
        if not isinstance(pkg, dict):
            continue
        remain, used, size = _package_remain_used(pkg)
        total_remain += remain
        total_used += used
        total_size += size
        packages.append({
            "name": pkg.get("PackageName", ""),
            "remain": remain,
            "used": used,
            "size": size,
        })

    # 补全 used
    if total_size > 0:
        derived = total_size - total_remain
        if derived < 0:
            derived = 0
        if derived > total_used:
            total_used = derived

    dosage = resp_data.get("TotalDosage", 0)
    if dosage and dosage > total_size:
        total_size = dosage
        derived = total_size - total_remain
        if derived > total_used:
            total_used = max(derived, 0)

    return {
        "total_remain": total_remain,
        "total_used": total_used,
        "total_size": total_size,
        "pack_count": len(packages),
        "packages": packages,
    }, None


def _package_remain_used(pkg):
    """提取单个套餐包的 remain/used/size（来自 billing.go packageRemainUsed）。"""
    # 优先使用 cycle 指标
    cycle_size = pkg.get("CycleCapacitySize", 0)
    if cycle_size and cycle_size > 0:
        remain = max(pkg.get("CycleCapacityRemain", 0), 0)
        if remain > cycle_size:
            remain = cycle_size
        used = cycle_size - remain
        explicit_used = pkg.get("CycleCapacityUsed", 0)
        if explicit_used > used:
            used = explicit_used
            if cycle_size >= used:
                remain = cycle_size - used
        return remain, used, cycle_size

    # cycle remain/used 但无 size
    cycle_remain = pkg.get("CycleCapacityRemain", 0)
    cycle_used = pkg.get("CycleCapacityUsed", 0)
    if cycle_remain > 0 or cycle_used > 0:
        remain = max(cycle_remain, 0)
        used = max(cycle_used, 0)
        size = remain + used
        cap_size = pkg.get("CapacitySize", 0)
        if cap_size > size:
            size = cap_size
            if size >= remain:
                used = size - remain
        return remain, used, size

    # lifetime 指标
    remain = max(pkg.get("CapacityRemain", 0), 0)
    used = max(pkg.get("CapacityUsed", 0), 0)
    size = pkg.get("CapacitySize", 0)
    if size <= 0:
        size = remain + used
    if used == 0 and size > remain:
        used = size - remain
    return remain, used, size


# ─── Trial 领取（来自 billing.go performTrialCall）────────────────────────────

def claim_trial(auth_data):
    """领取 Global 账号一次性 trial 专家加油包。返回 (result, error)。"""
    if not is_global(auth_data):
        return {"success": False, "message": "仅 Global 账号可领取 trial"}, None

    base = billing_base(auth_data)
    headers = billing_headers(auth_data)
    data, status, err = http_post_json(base + EP_TRIAL_CLAIM, body={}, headers=headers)

    if err:
        # code=14051 表示已领取过
        if "14051" in str(err):
            return {
                "success": False,
                "message": "已领取过专家加油包",
                "already_claimed": True,
            }, None
        return {"success": False, "message": err}, None

    if not isinstance(data, dict):
        data = {}
    data["success"] = True
    return data, None


# ─── 输出格式化 ────────────────────────────────────────────────────────────────

def _format_token(token):
    """脱敏显示 token。"""
    if not token:
        return "(空)"
    return token[:16] + "...(已隐藏)"


def _format_expires(expires_at):
    """格式化过期时间。"""
    if not expires_at:
        return "(未知)"
    dt = datetime.fromtimestamp(expires_at)
    now = datetime.now()
    remaining = expires_at - time.time()
    if remaining > 0:
        hours = int(remaining // 3600)
        return f"{dt.strftime('%Y-%m-%d %H:%M')} (剩余 {hours}h)"
    return f"{dt.strftime('%Y-%m-%d %H:%M')} (已过期)"


def print_signin_result(result, verbose=False):
    """格式化打印签到结果。"""
    nickname = result.get("nickname", "")
    uid = result.get("uid", "")
    region = result.get("region", "?")
    tag = f"[{region.upper()}]"

    if result.get("skipped"):
        reason = result.get("reason", "")
        msg = result.get("message", "")
        print(f"  {tag} {nickname}({uid}): 跳过 — {msg} [{reason}]")
    elif result.get("error"):
        print(f"  {tag} {nickname}({uid}): 失败 — {result['error']}")
    elif result.get("success"):
        msg = result.get("message", "签到成功")
        credits = result.get("credits", "")
        credit_str = f"，当前积分: {credits}" if credits != "" else ""
        print(f"  {tag} {nickname}({uid}): 成功 — {msg}{credit_str}")
    else:
        msg = result.get("message", "未知")
        print(f"  {tag} {nickname}({uid}): 失败 — {msg}")

    if verbose and result.get("checkin_status"):
        ci = result["checkin_status"]
        print(f"         连签: {ci.get('streak_days', 0)} 天 | "
              f"周签到: {ci.get('week_checkin_days', 0)} 天 | "
              f"今日积分: {ci.get('daily_credit', 0)} | "
              f"总积分: {ci.get('total_credits', 0)}")


def print_credit_report(uid_filter, auth_dir, as_json=False):
    """打印积分日报。"""
    files = list_auth_files(auth_dir)
    if not files:
        print("未找到凭证文件")
        return

    reports = []
    for path in files:
        try:
            auth_data = load_auth(path)
        except (json.JSONDecodeError, ValueError, OSError) as e:
            print(f"  跳过 {os.path.basename(path)}: {e}")
            continue

        acct = auth_data.get("account", {})
        uid = acct.get("uid", "") or uid_from_filename(path)

        if uid_filter and uid != uid_filter:
            continue

        auth_data = ensure_token_valid(path, auth_data)
        credits, err = fetch_user_resource(auth_data)

        report = {
            "uid": uid,
            "nickname": acct.get("nickname", ""),
            "region": "global" if is_global(auth_data) else "cn",
            "disabled": auth_data.get("disabled", False),
        }

        if err:
            report["error"] = err
            report["total_remain"] = 0
        else:
            report["total_remain"] = credits.get("total_remain", 0)
            report["total_used"] = credits.get("total_used", 0)
            report["total_size"] = credits.get("total_size", 0)
            report["pack_count"] = credits.get("pack_count", 0)
            report["packages"] = credits.get("packages", [])

        reports.append(report)

    if as_json:
        print(json.dumps(reports, indent=2, ensure_ascii=False))
        return

    if not reports:
        print(f"未找到 UID 为 {uid_filter} 的账号")
        return

    # 人类可读格式
    print(f"\n{'='*60}")
    print(f"  WorkBuddy 积分日报 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    for r in reports:
        tag = f"[{r['region'].upper()}]"
        nick = r.get("nickname", "")
        uid = r.get("uid", "")
        disabled = " [DISABLED]" if r.get("disabled") else ""
        if r.get("error"):
            print(f"  {tag} {nick}({uid}){disabled}: 查询失败 — {r['error']}")
        else:
            remain = r.get("total_remain", 0)
            used = r.get("total_used", 0)
            size = r.get("total_size", 0)
            packs = r.get("pack_count", 0)
            print(f"  {tag} {nick}({uid}){disabled}: {remain} / {size} "
                  f"(已用 {used}, {packs} 个套餐包)")
            for pkg in r.get("packages", []):
                print(f"         - {pkg['name']}: {pkg['remain']}/{pkg['size']}")
    print(f"{'='*60}")
    total = sum(r.get("total_remain", 0) for r in reports if not r.get("error"))
    print(f"  总剩余积分: {total} ({len(reports)} 个账号)")
    print(f"{'='*60}\n")


# ─── JWT 辅助（判断 token 区域，来自 models.go isGlobalToken）─────────────────

def is_global_token(access_token):
    """解码 JWT iss 判断是否 Global token。"""
    parts = access_token.split(".")
    if len(parts) < 2:
        return False
    payload = parts[1]
    # base64url padding
    pad = len(payload) % 4
    if pad:
        payload += "=" * (4 - pad)
    try:
        raw = base64.urlsafe_b64decode(payload)
        claims = json.loads(raw)
        iss = (claims.get("iss") or "").lower()
        return "workbuddy.ai" in iss
    except Exception:
        return False


# ─── 辅助类型解析 ──────────────────────────────────────────────────────────────

def _bool(d, *keys):
    for k in keys:
        if k in d:
            v = d[k]
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return v != 0
            if isinstance(v, str):
                return v.lower() in ("true", "1")
    return False


def _int(d, *keys):
    for k in keys:
        if k in d:
            try:
                return int(d[k])
            except (TypeError, ValueError):
                pass
    return 0


def _str(d, *keys):
    for k in keys:
        if k in d and isinstance(d[k], str):
            return d[k]
    return ""


def uid_from_filename(path):
    """从 workbuddy-<uid>.json 提取 uid。"""
    import re
    m = re.search(r"workbuddy-(.+)\.json", os.path.basename(path))
    return m.group(1) if m else ""


# ─── Bark 推送通知 ────────────────────────────────────────────────────────────

def send_bark_notification(success_n, already_n, fail_n, global_n, skipped_n, results):
    """通过 Bark 推送签到结果通知。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 构造通知标题
    total = success_n + already_n + fail_n + global_n + skipped_n
    if fail_n > 0:
        title = f"WorkBuddy签到 {success_n}成功/{fail_n}失败"
        icon = "❌"
    elif success_n > 0:
        title = f"WorkBuddy签到 {success_n}成功"
        icon = "✅"
    elif already_n > 0:
        title = f"WorkBuddy签到 今日已签"
        icon = "✓"
    else:
        title = "WorkBuddy签到 完成"
        icon = "📋"

    # 构造通知正文
    lines = []
    for r in results:
        nickname = r.get("nickname", "")
        region = r.get("region", "?").upper()
        if r.get("error"):
            lines.append(f"[{region}] {nickname}: 失败 - {r['error'][:50]}")
        elif r.get("skipped"):
            reason = r.get("reason", "")
            msg = r.get("message", "")
            if reason == "already":
                ci = r.get("checkin_status", {})
                streak = ci.get("streak_days", 0)
                credits = r.get("credits", "")
                lines.append(f"[{region}] {nickname}: 已签(连{streak}天) {credits}")
            else:
                lines.append(f"[{region}] {nickname}: {msg}")
        elif r.get("success"):
            msg = r.get("message", "成功")
            credits = r.get("credits", "")
            ci = r.get("checkin_status", {})
            streak = ci.get("streak_days", 0)
            lines.append(f"[{region}] {nickname}: {msg} 连{streak}天 {credits}")

    summary = f"成功{success_n} 已签{already_n} 失败{fail_n}"
    body = "\n".join(lines) if lines else "无账号"
    body += f"\n{summary}"

    # 发送到 Bark
    bark_base = BARK_URL.rstrip("/")
    payload = {
        "title": f"{icon} {title}",
        "body": body,
        "group": "WorkBuddy",
        "icon": "https://www.codebuddy.cn/favicon.ico",
    }

    # 失败时设置铃声和级别
    if fail_n > 0:
        payload["sound"] = "alarm"
        payload["level"] = "timeSensitive"

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(bark_base, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                print(f"  [通知] Bark 推送成功")
            else:
                print(f"  [通知] Bark 推送失败 (HTTP {resp.status})")
    except Exception as e:
        print(f"  [通知] Bark 推送异常: {e}")


# ─── 主命令 ────────────────────────────────────────────────────────────────────

def cmd_signin(args):
    """批量签到。"""
    auth_dir = args.auth_dir or "auths"
    files = list_auth_files(auth_dir)
    if not files:
        print(f"未在 {auth_dir}/ 找到凭证文件")
        print("请将 workbuddy-<uid>.json 放入该目录")
        return

    print(f"\n{'='*60}")
    print(f"  WorkBuddy 批量签到 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  共 {len(files)} 个账号")
    print(f"{'='*60}\n")

    success_n = fail_n = already_n = global_n = skipped_n = 0
    results = []

    for path in files:
        try:
            auth_data = load_auth(path)
        except (json.JSONDecodeError, ValueError, OSError) as e:
            print(f"  跳过 {os.path.basename(path)}: {e}")
            skipped_n += 1
            continue

        result = checkin_one_account(path, auth_data)
        print_signin_result(result, verbose=True)
        results.append(result)

        if result.get("error"):
            fail_n += 1
        elif result.get("skipped"):
            reason = result.get("reason", "")
            if reason == "already":
                already_n += 1
            elif reason == "global":
                global_n += 1
            else:
                skipped_n += 1
        elif result.get("success"):
            success_n += 1
        else:
            fail_n += 1

    print(f"\n{'─'*60}")
    print(f"  汇总: 成功 {success_n} | 已签 {already_n} | 失败 {fail_n} | "
          f"Global跳过 {global_n} | 其他跳过 {skipped_n}")
    print(f"{'='*60}\n")

    # Bark 推送通知
    if BARK_URL:
        send_bark_notification(success_n, already_n, fail_n, global_n, skipped_n, results)


def cmd_credit(args):
    """积分查询。"""
    auth_dir = args.auth_dir or "auths"
    uid_filter = args.uid
    # 智能识别：若 uid 实为目录路径（已存在或含分隔符），则视作 auth_dir
    if uid_filter and (os.path.isdir(uid_filter) or "/" in uid_filter or "\\" in uid_filter):
        auth_dir = uid_filter
        uid_filter = None
    elif uid_filter and uid_filter.startswith("-"):
        uid_filter = None
    as_json = args.json
    print_credit_report(uid_filter, auth_dir, as_json=as_json)


def cmd_trial(args):
    """领取 trial。"""
    auth_dir = args.auth_dir or "auths"
    uid_filter = args.uid
    if not uid_filter:
        print("请指定 UID: python3 workbuddy_checkin.py trial <uid>")
        return

    files = list_auth_files(auth_dir)
    for path in files:
        try:
            auth_data = load_auth(path)
        except (json.JSONDecodeError, ValueError, OSError):
            continue

        uid = auth_data.get("account", {}).get("uid", "") or uid_from_filename(path)
        if uid != uid_filter:
            continue

        auth_data = ensure_token_valid(path, auth_data)
        result, err = claim_trial(auth_data)
        nickname = auth_data.get("account", {}).get("nickname", "")

        if err:
            print(f"  {nickname}({uid}): 失败 — {err}")
        elif result.get("success"):
            print(f"  {nickname}({uid}): Trial 领取成功")
            credits, _ = fetch_user_resource(auth_data)
            if credits:
                print(f"  当前积分: {credits.get('total_remain', 0)}")
        else:
            already = " (已领取)" if result.get("already_claimed") else ""
            print(f"  {nickname}({uid}): {result.get('message', '未知')}{already}")
        return

    print(f"未找到 UID 为 {uid_filter} 的账号")


def cmd_refresh(args):
    """强制刷新所有 token。"""
    auth_dir = args.auth_dir or "auths"
    files = list_auth_files(auth_dir)
    if not files:
        print(f"未在 {auth_dir}/ 找到凭证文件")
        return

    print(f"\n  刷新所有 token — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    for path in files:
        try:
            auth_data = load_auth(path)
        except (json.JSONDecodeError, ValueError, OSError) as e:
            print(f"  跳过 {os.path.basename(path)}: {e}")
            continue

        nickname = auth_data.get("account", {}).get("nickname", "")
        uid = auth_data.get("account", {}).get("uid", "") or uid_from_filename(path)
        ok, msg = refresh_token(path, auth_data)
        status = "✓" if ok else "✗"
        print(f"  {status} {nickname}({uid}): {msg}")
    print()


def cmd_cron(args):
    """定时模式：每天指定时间点执行签到。"""
    hour = args.hour
    auth_dir = args.auth_dir or "auths"

    print(f"\n  定时签到模式 — 每天 {hour:02d}:00 执行")
    print(f"  凭证目录: {auth_dir}/")
    print(f"  按 Ctrl+C 退出\n")

    while True:
        now = datetime.now()
        # 计算下次执行时间
        next_run = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run = next_run.replace(day=now.day + 1)

        wait_seconds = (next_run - now).total_seconds()
        print(f"  下次执行: {next_run.strftime('%Y-%m-%d %H:%M')} (等待 {int(wait_seconds)} 秒)")

        # 睡眠等待（每小时检查一次，避免长睡眠卡死）
        while datetime.now() < next_run:
            time.sleep(min(3600, (next_run - datetime.now()).total_seconds()))

        print(f"\n  [执行] {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        # 复用 signin 逻辑
        args.auth_dir = auth_dir
        args.uid = None
        args.json = False
        cmd_signin(args)


def main():
    parser = argparse.ArgumentParser(
        description="WorkBuddy (CodeBuddy) 签到 / 积分查询脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 workbuddy_checkin.py signin              # 批量签到
  python3 workbuddy_checkin.py credit              # 积分日报
  python3 workbuddy_checkin.py credit abc123       # 指定账号积分
  python3 workbuddy_checkin.py credit -j           # JSON 格式积分
  python3 workbuddy_checkin.py trial abc123        # 领取 trial (Global)
  python3 workbuddy_checkin.py refresh             # 刷新所有 token
  python3 workbuddy_checkin.py cron 9              # 每天早上9点签到
        """,
    )
    sub = parser.add_subparsers(dest="command")

    p_signin = sub.add_parser("signin", help="批量签到")
    p_signin.add_argument("auth_dir", nargs="?", default="auths", help="凭证目录 (默认 auths)")

    p_credit = sub.add_parser("credit", help="积分查询")
    p_credit.add_argument("uid", nargs="?", default=None, help="指定账号 UID")
    p_credit.add_argument("auth_dir", nargs="?", default="auths", help="凭证目录")
    p_credit.add_argument("-j", "--json", action="store_true", help="JSON 格式输出")

    p_trial = sub.add_parser("trial", help="领取 trial (Global 账号)")
    p_trial.add_argument("uid", help="账号 UID")
    p_trial.add_argument("auth_dir", nargs="?", default="auths", help="凭证目录")

    p_refresh = sub.add_parser("refresh", help="强制刷新所有 token")
    p_refresh.add_argument("auth_dir", nargs="?", default="auths", help="凭证目录")

    p_cron = sub.add_parser("cron", help="定时模式")
    p_cron.add_argument("hour", type=int, help="每天执行的小时 (0-23)")
    p_cron.add_argument("auth_dir", nargs="?", default="auths", help="凭证目录")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "signin": cmd_signin,
        "credit": cmd_credit,
        "trial": cmd_trial,
        "refresh": cmd_refresh,
        "cron": cmd_cron,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
