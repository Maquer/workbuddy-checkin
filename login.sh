#!/usr/bin/env bash
# ============================================================================
# workbuddy-checkin/login.sh — CodeBuddy (WorkBuddy) 交互式登录脚本
#
# 通过浏览器 OAuth 登录获取 accessToken / refreshToken，自动保存凭证文件。
# 凭证文件格式与 workbuddy_checkin.py 完全兼容。
#
# 用法:
#   ./login.sh                # 交互式选择区域
#   ./login.sh cn             # 直接登录 CN (codebuddy.cn)
#   ./login.sh global         # 直接登录 Global (workbuddy.ai)
#
# 依赖: curl, python3 (用于 JSON 解析)
# ============================================================================

set -euo pipefail

# ─── 颜色 ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m' # No Color

# ─── 配置 ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTH_DIR="${SCRIPT_DIR}/auths"

# CN 域名
CN_AUTH_BASE="https://copilot.tencent.com"
CN_BILLING_DOMAIN="codebuddy.cn"

# Global 域名
GLOBAL_AUTH_BASE="https://www.workbuddy.ai"
GLOBAL_BILLING_DOMAIN="workbuddy.ai"

# 客户端标识
CLIENT_UA="CLI/2.63.2 CodeBuddy/2.63.2"

# 轮询配置
POLL_INTERVAL=3        # 秒
POLL_TIMEOUT=300       # 5 分钟超时

# ─── 工具函数 ──────────────────────────────────────────────────────────────────

print_banner() {
    echo -e "${CYAN}"
    echo "  ╔══════════════════════════════════════════════════╗"
    echo "  ║       WorkBuddy / CodeBuddy 交互式登录          ║"
    echo "  ╚══════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

print_info()    { echo -e "  ${BLUE}[INFO]${NC}  $1"; }
print_success() { echo -e "  ${GREEN}[OK]${NC}    $1"; }
print_warn()    { echo -e "  ${YELLOW}[WARN]${NC}  $1"; }
print_error()   { echo -e "  ${RED}[ERR]${NC}   $1"; }
print_step()    { echo -e "\n  ${BOLD}── 步骤 $1: $2 ──${NC}"; }

# JSON 解析（使用 python3，兼容性最好）
json_get() {
    local json_data="$1"
    local key="$2"
    python3 -c "
import json, sys
try:
    data = json.loads('''$json_data''')
    # 支持嵌套 key: data.field
    parts = '$key'.split('.')
    val = data
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p, '')
        else:
            val = ''
        if val == '' and p not in (data if isinstance(data, dict) else {}):
            break
    print(val if val is not None else '')
except Exception:
    print('')
" 2>/dev/null
}

# 更可靠的 JSON 解析（通过 stdin 传递，避免引号转义问题）
json_get_safe() {
    local key="$1"
    python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    parts = '$key'.split('.')
    val = data
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p, '')
        elif isinstance(val, list):
            try:
                val = val[int(p)]
            except (ValueError, IndexError):
                val = ''
        else:
            val = ''
    print(val if val is not None else '')
except Exception as e:
    print('')
" 2>/dev/null
}

# 获取 Unix 时间戳
now_timestamp() {
    date +%s
}

# 打开浏览器
open_browser() {
    local url="$1"
    if command -v xdg-open &>/dev/null; then
        xdg-open "$url" 2>/dev/null &
    elif command -v open &>/dev/null; then
        open "$url" 2>/dev/null &
    elif command -v wslview &>/dev/null; then
        wslview "$url" 2>/dev/null &
    else
        print_warn "无法自动打开浏览器，请手动复制链接"
    fi
}

# ─── 选择区域 ──────────────────────────────────────────────────────────────────

select_region() {
    local region="${1:-}"

    if [[ -n "$region" ]]; then
        region=$(echo "$region" | tr '[:upper:]' '[:lower:]')
        case "$region" in
            cn|china|codebuddy)
                SELECTED_REGION="cn"
                return 0
                ;;
            global|intl|workbuddy)
                SELECTED_REGION="global"
                return 0
                ;;
            *)
                print_error "未知区域: $region (可用: cn, global)"
                exit 1
                ;;
        esac
    fi

    echo -e "\n  ${BOLD}请选择登录区域:${NC}\n"
    echo -e "  ${CYAN}1${NC})  CN      — 腾讯 CodeBuddy (codebuddy.cn)${DIM} — 支持每日签到${NC}"
    echo -e "  ${CYAN}2${NC})  Global  — WorkBuddy 国际版 (workbuddy.ai)${DIM} — 支持 Trial 领取${NC}"
    echo ""

    while true; do
        read -rp "  请选择 [1/2] (默认 1): " choice
        choice="${choice:-1}"
        case "$choice" in
            1) SELECTED_REGION="cn"; break ;;
            2) SELECTED_REGION="global"; break ;;
            *) echo -e "  ${RED}无效选择，请输入 1 或 2${NC}" ;;
        esac
    done
}

# ─── 根据区域设置端点 ──────────────────────────────────────────────────────────

setup_endpoints() {
    if [[ "$SELECTED_REGION" == "cn" ]]; then
        AUTH_BASE="$CN_AUTH_BASE"
        BILLING_DOMAIN="$CN_BILLING_DOMAIN"
        REGION_LABEL="CN (codebuddy.cn)"
    else
        AUTH_BASE="$GLOBAL_AUTH_BASE"
        BILLING_DOMAIN="$GLOBAL_BILLING_DOMAIN"
        REGION_LABEL="Global (workbuddy.ai)"
    fi

    EP_AUTH_STATE="${AUTH_BASE}/v2/plugin/auth/state?platform=CLI"
    EP_AUTH_TOKEN="${AUTH_BASE}/v2/plugin/auth/token?state="
    EP_LOGIN_ACCT="${AUTH_BASE}/v2/plugin/login/account?state="
}

# ─── 步骤 1: 获取 Auth State ──────────────────────────────────────────────────

step1_get_auth_state() {
    print_step 1 "获取登录授权状态"

    print_info "请求授权端点: ${AUTH_BASE}"
    print_info "区域: ${REGION_LABEL}"

    local response
    local http_code

    response=$(curl -sS \
        -X POST \
        "$EP_AUTH_STATE" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json" \
        -H "User-Agent: ${CLIENT_UA}" \
        -d '{}' \
        -w "\n%{http_code}" \
        2>&1) || {
        print_error "请求失败，请检查网络连接"
        exit 1
    }

    http_code=$(echo "$response" | tail -1)
    local body=$(echo "$response" | sed '$d')

    if [[ "$http_code" != "200" ]]; then
        print_error "HTTP $http_code — 获取授权状态失败"
        echo -e "  ${DIM}响应: $(echo "$body" | head -c 200)${NC}"
        exit 1
    fi

    # 解析 code
    local code
    code=$(echo "$body" | json_get_safe "code")

    if [[ "$code" != "0" ]]; then
        print_error "业务错误 (code=$code): $(echo "$body" | json_get_safe 'msg')"
        exit 1
    fi

    AUTH_STATE=$(echo "$body" | json_get_safe "data.state")
    AUTH_URL=$(echo "$body" | json_get_safe "data.authUrl")

    # 尝试其他可能的字段名（camelCase / snake_case 回退）
    if [[ -z "$AUTH_STATE" || "$AUTH_STATE" == "" ]]; then
        AUTH_STATE=$(echo "$body" | json_get_safe "data.State")
    fi
    if [[ -z "$AUTH_URL" || "$AUTH_URL" == "" ]]; then
        AUTH_URL=$(echo "$body" | json_get_safe "data.auth_url")
    fi
    if [[ -z "$AUTH_URL" || "$AUTH_URL" == "" ]]; then
        AUTH_URL=$(echo "$body" | json_get_safe "data.AuthURL")
    fi

    if [[ -z "$AUTH_STATE" || "$AUTH_STATE" == "" ]]; then
        print_error "未能从响应中提取 state"
        echo -e "  ${DIM}响应: $(echo "$body" | head -c 300)${NC}"
        exit 1
    fi

    print_success "授权状态获取成功"
    print_info "State: ${AUTH_STATE}"
}

# ─── 步骤 2: 打开浏览器登录 ────────────────────────────────────────────────────

step2_open_browser() {
    print_step 2 "浏览器登录"

    if [[ -n "$AUTH_URL" && "$AUTH_URL" != "" ]]; then
        print_info "登录链接:"
        echo -e "  ${CYAN}${AUTH_URL}${NC}"
        echo ""
    else
        # 如果没有 auth_url，构造一个
        AUTH_URL="${AUTH_BASE}/login?state=${AUTH_STATE}&platform=CLI"
        print_info "构造登录链接:"
        echo -e "  ${CYAN}${AUTH_URL}${NC}"
        echo ""
    fi

    print_info "正在打开浏览器..."
    open_browser "$AUTH_URL"

    echo -e "  ${YELLOW}请在浏览器中完成登录操作${NC}"
    echo -e "  ${DIM}登录成功后，脚本将自动检测并获取 Token${NC}"
    echo ""
    read -rp "  按回车键开始检测登录状态（或等待自动检测）..." _
}

# ─── 步骤 3: 轮询 Token ───────────────────────────────────────────────────────

step3_poll_token() {
    print_step 3 "等待登录完成（轮询 Token）"

    local elapsed=0
    local poll_url="${EP_AUTH_TOKEN}${AUTH_STATE}"

    print_info "轮询间隔: ${POLL_INTERVAL}s | 超时: ${POLL_TIMEOUT}s"
    print_info "如果浏览器未自动打开，请手动访问上面的登录链接"
    echo ""

    while [[ $elapsed -lt $POLL_TIMEOUT ]]; do
        printf "\r  ${DIM}等待登录中... 已等待 %ds / %ds${NC}" "$elapsed" "$POLL_TIMEOUT"

        local response
        local http_code

        response=$(curl -sS \
            -X GET \
            "$poll_url" \
            -H "Accept: application/json" \
            -H "User-Agent: ${CLIENT_UA}" \
            -w "\n%{http_code}" \
            2>&1) || {
            # 网络错误，等待重试
            sleep "$POLL_INTERVAL"
            elapsed=$((elapsed + POLL_INTERVAL))
            continue
        }

        http_code=$(echo "$response" | tail -1)
        local body=$(echo "$response" | sed '$d')

        # HTTP 4xx/5xx — 登录尚未完成或服务错误，等待重试
        if [[ "$http_code" =~ ^[45][0-9][0-9]$ ]]; then
            sleep "$POLL_INTERVAL"
            elapsed=$((elapsed + POLL_INTERVAL))
            continue
        fi

        # HTTP 200 — 检查业务码
        if [[ "$http_code" == "200" ]]; then
            local code
            code=$(echo "$body" | json_get_safe "code")

            # code=0 表示登录成功，拿到 token
            if [[ "$code" == "0" ]]; then
                # 成功获取 token（兼容 snake_case 和 camelCase）
                ACCESS_TOKEN=$(echo "$body" | json_get_safe "data.access_token")
                REFRESH_TOKEN=$(echo "$body" | json_get_safe "data.refresh_token")
                EXPIRES_IN=$(echo "$body" | json_get_safe "data.expires_in")
                TOKEN_DOMAIN=$(echo "$body" | json_get_safe "data.domain")

                # camelCase 回退
                if [[ -z "$ACCESS_TOKEN" || "$ACCESS_TOKEN" == "" ]]; then
                    ACCESS_TOKEN=$(echo "$body" | json_get_safe "data.accessToken")
                    REFRESH_TOKEN=$(echo "$body" | json_get_safe "data.refreshToken")
                    EXPIRES_IN=$(echo "$body" | json_get_safe "data.expiresIn")
                    TOKEN_DOMAIN=$(echo "$body" | json_get_safe "data.domain")
                fi

                if [[ -n "$ACCESS_TOKEN" && "$ACCESS_TOKEN" != "" ]]; then
                    echo ""
                    echo ""
                    print_success "登录成功！Token 已获取"
                    print_info "Access Token: ${ACCESS_TOKEN:0:32}..."
                    print_info "Expires In: ${EXPIRES_IN}s"
                    print_info "Domain: ${TOKEN_DOMAIN:-$BILLING_DOMAIN}"

                    # 计算过期时间戳
                    if [[ -n "$EXPIRES_IN" && "$EXPIRES_IN" -gt 0 ]] 2>/dev/null; then
                        EXPIRES_AT=$(( $(now_timestamp) + EXPIRES_IN ))
                    else
                        EXPIRES_AT=0
                    fi
                    return 0
                fi
            fi
        fi

        sleep "$POLL_INTERVAL"
        elapsed=$((elapsed + POLL_INTERVAL))
    done

    echo ""
    echo ""
    print_error "登录超时（${POLL_TIMEOUT}s），请重新运行脚本"
    exit 1
}

# ─── 步骤 4: 获取账号信息 ──────────────────────────────────────────────────────

step4_get_account() {
    print_step 4 "获取账号信息"

    local acct_url="${EP_LOGIN_ACCT}${AUTH_STATE}"

    local response
    local http_code

    response=$(curl -sS \
        -X GET \
        "$acct_url" \
        -H "Accept: application/json" \
        -H "User-Agent: ${CLIENT_UA}" \
        -H "Authorization: Bearer ${ACCESS_TOKEN}" \
        -w "\n%{http_code}" \
        2>&1) || {
        print_warn "获取账号信息失败，将使用默认值"
        ACCOUNT_UID="unknown"
        ACCOUNT_ENTERPRISE_ID=""
        ACCOUNT_NICKNAME="Unknown"
        return 0
    }

    http_code=$(echo "$response" | tail -1)
    local body=$(echo "$response" | sed '$d')

    if [[ "$http_code" != "200" ]]; then
        print_warn "HTTP $http_code — 获取账号信息失败，将使用默认值"
        ACCOUNT_UID="unknown"
        ACCOUNT_ENTERPRISE_ID=""
        ACCOUNT_NICKNAME="Unknown"
        return 0
    fi

    local code
    code=$(echo "$body" | json_get_safe "code")

    if [[ "$code" != "0" ]]; then
        print_warn "业务错误 (code=$code)，将使用默认值"
        ACCOUNT_UID="unknown"
        ACCOUNT_ENTERPRISE_ID=""
        ACCOUNT_NICKNAME="Unknown"
        return 0
    fi

    # 解析账号信息（兼容 snake_case 和 camelCase）
    ACCOUNT_UID=$(echo "$body" | json_get_safe "data.uid")
    ACCOUNT_ENTERPRISE_ID=$(echo "$body" | json_get_safe "data.enterprise_id")
    ACCOUNT_NICKNAME=$(echo "$body" | json_get_safe "data.nickname")

    # camelCase 回退
    if [[ -z "$ACCOUNT_UID" || "$ACCOUNT_UID" == "" ]]; then
        ACCOUNT_UID=$(echo "$body" | json_get_safe "data.UID")
    fi
    if [[ -z "$ACCOUNT_ENTERPRISE_ID" || "$ACCOUNT_ENTERPRISE_ID" == "" ]]; then
        ACCOUNT_ENTERPRISE_ID=$(echo "$body" | json_get_safe "data.enterpriseId")
    fi
    if [[ -z "$ACCOUNT_ENTERPRISE_ID" || "$ACCOUNT_ENTERPRISE_ID" == "" ]]; then
        ACCOUNT_ENTERPRISE_ID=$(echo "$body" | json_get_safe "data.EnterpriseID")
    fi
    if [[ -z "$ACCOUNT_NICKNAME" || "$ACCOUNT_NICKNAME" == "" ]]; then
        ACCOUNT_NICKNAME=$(echo "$body" | json_get_safe "data.Nickname")
    fi

    # 如果 UID 为空，尝试从 JWT 解码
    if [[ -z "$ACCOUNT_UID" || "$ACCOUNT_UID" == "" ]]; then
        print_warn "API 未返回 UID，尝试从 JWT 解码..."
        ACCOUNT_UID=$(decode_jwt_uid "$ACCESS_TOKEN")
    fi

    if [[ -z "$ACCOUNT_UID" || "$ACCOUNT_UID" == "" ]]; then
        ACCOUNT_UID="unknown"
        print_warn "无法获取 UID，将使用 'unknown'"
    fi

    if [[ -z "$ACCOUNT_NICKNAME" || "$ACCOUNT_NICKNAME" == "" ]]; then
        ACCOUNT_NICKNAME="User-${ACCOUNT_UID}"
    fi

    # 如果 domain 未从 token 响应中获取，使用区域默认值
    if [[ -z "$TOKEN_DOMAIN" || "$TOKEN_DOMAIN" == "" ]]; then
        TOKEN_DOMAIN="$BILLING_DOMAIN"
    fi

    print_success "账号信息获取成功"
    print_info "UID: ${ACCOUNT_UID}"
    print_info "昵称: ${ACCOUNT_NICKNAME}"
    print_info "企业 ID: ${ACCOUNT_ENTERPRISE_ID:-（无）}"
}

# ─── 从 JWT 解码 UID ─────────────────────────────────────────────────────────

decode_jwt_uid() {
    local token="$1"
    python3 -c "
import base64, json, sys
token = '''$token'''
parts = token.split('.')
if len(parts) < 2:
    print('')
    sys.exit(0)
payload = parts[1]
pad = len(payload) % 4
if pad:
    payload += '=' * (4 - pad)
try:
    claims = json.loads(base64.urlsafe_b64decode(payload))
    uid = claims.get('uid', claims.get('sub', claims.get('user_id', '')))
    print(uid if uid else '')
except Exception:
    print('')
" 2>/dev/null
}

# ─── 步骤 5: 保存凭证文件 ──────────────────────────────────────────────────────

step5_save_credentials() {
    print_step 5 "保存凭证文件"

    # 创建 auths 目录
    mkdir -p "$AUTH_DIR"

    local filename="workbuddy-${ACCOUNT_UID}.json"
    local filepath="${AUTH_DIR}/${filename}"

    # 检查是否已存在
    if [[ -f "$filepath" ]]; then
        print_warn "凭证文件已存在: $filename"
        read -rp "  是否覆盖? [y/N] " overwrite
        if [[ ! "$overwrite" =~ ^[Yy] ]]; then
            print_info "已取消，凭证未保存"
            exit 0
        fi
    fi

    # 使用 python3 生成 JSON（确保格式正确）
    python3 -c "
import json, os
data = {
    'type': 'workbuddy',
    'provider': 'workbuddy',
    'disabled': False,
    'note': '',
    'auth': {
        'accessToken': '${ACCESS_TOKEN}',
        'refreshToken': '${REFRESH_TOKEN}',
        'expiresAt': ${EXPIRES_AT:-0},
        'domain': '${TOKEN_DOMAIN}'
    },
    'account': {
        'uid': '${ACCOUNT_UID}',
        'enterpriseId': '${ACCOUNT_ENTERPRISE_ID}',
        'nickname': '${ACCOUNT_NICKNAME}'
    }
}
filepath = '${filepath}'
# 原子写入
tmp = filepath + '.tmp'
with open(tmp, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write('\n')
os.replace(tmp, filepath)
print(f'凭证已保存到: {filepath}')
" 2>&1

    if [[ $? -ne 0 ]]; then
        print_error "保存凭证失败"
        exit 1
    fi

    # 设置文件权限
    chmod 600 "$filepath" 2>/dev/null || true

    print_success "凭证文件已保存"
    print_info "路径: ${filepath}"
    print_info "权限: 600 (仅所有者可读写)"
}

# ─── 步骤 6: 验证凭证 ──────────────────────────────────────────────────────────

step6_verify() {
    print_step 6 "验证凭证"

    print_info "正在查询积分以验证凭证有效性..."

    cd "$SCRIPT_DIR"
    if python3 workbuddy_checkin.py credit "${ACCOUNT_UID}" 2>&1; then
        echo ""
        print_success "凭证验证完成"
    else
        print_warn "验证时出现警告（可能是网络问题），但凭证已保存"
    fi
}

# ─── 显示完成信息 ──────────────────────────────────────────────────────────────

show_summary() {
    echo ""
    echo -e "  ${GREEN}${BOLD}══════════════════════════════════════════════════${NC}"
    echo -e "  ${GREEN}${BOLD}  登录完成！${NC}"
    echo -e "  ${GREEN}${BOLD}══════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "  ${BOLD}账号摘要:${NC}"
    echo -e "    区域:     ${CYAN}${REGION_LABEL}${NC}"
    echo -e "    UID:      ${CYAN}${ACCOUNT_UID}${NC}"
    echo -e "    昵称:     ${CYAN}${ACCOUNT_NICKNAME}${NC}"
    echo -e "    企业 ID:  ${CYAN}${ACCOUNT_ENTERPRISE_ID:-（无）}${NC}"
    echo -e "    过期时间: ${CYAN}$(python3 -c "from datetime import datetime; print(datetime.fromtimestamp(${EXPIRES_AT:-0}).strftime('%Y-%m-%d %H:%M'))" 2>/dev/null || echo '未知')${NC}"
    echo ""
    echo -e "  ${BOLD}后续操作:${NC}"
    echo -e "    ${DIM}# 每日签到${NC}"
    echo -e "    ${CYAN}python3 workbuddy_checkin.py signin${NC}"
    echo ""
    echo -e "    ${DIM}# 查询积分${NC}"
    echo -e "    ${CYAN}python3 workbuddy_checkin.py credit${NC}"
    echo ""
    echo -e "    ${DIM}# 领取 Trial (仅 Global)${NC}"
    echo -e "    ${CYAN}python3 workbuddy_checkin.py trial ${ACCOUNT_UID}${NC}"
    echo ""
    echo -e "    ${DIM}# 定时签到 (每天 9 点)${NC}"
    echo -e "    ${CYAN}python3 workbuddy_checkin.py cron 9${NC}"
    echo ""

    # 提示是否继续登录其他账号
    read -rp "  是否继续登录其他账号? [y/N] " continue_login
    if [[ "$continue_login" =~ ^[Yy] ]]; then
        echo ""
        exec "$0"
    fi

    echo -e "\n  ${DIM}再见！${NC}\n"
}

# ─── 手动模式（直接输入 Token）────────────────────────────────────────────────

manual_mode() {
    echo -e "\n  ${BOLD}── 手动 Token 输入模式 ──${NC}"
    echo -e "  ${DIM}适用于已有 Token 但需要格式化保存的场景${NC}\n"

    read -rp "  Access Token: " access_token
    read -rp "  Refresh Token: " refresh_token
    read -rp "  UID: " uid
    read -rp "  昵称 (可选): " nickname
    read -rp "  企业 ID (可选): " enterprise_id
    read -rp "  Domain [codebuddy.cn/workbuddy.ai]: " domain

    domain="${domain:-codebuddy.cn}"
    nickname="${nickname:-User-${uid}}"

    if [[ -z "$access_token" || -z "$refresh_token" || -z "$uid" ]]; then
        print_error "Access Token、Refresh Token 和 UID 为必填项"
        exit 1
    fi

    ACCESS_TOKEN="$access_token"
    REFRESH_TOKEN="$refresh_token"
    ACCOUNT_UID="$uid"
    ACCOUNT_NICKNAME="$nickname"
    ACCOUNT_ENTERPRISE_ID="$enterprise_id"
    TOKEN_DOMAIN="$domain"
    EXPIRES_AT=0
    SELECTED_REGION=""

    if [[ "$domain" == "workbuddy.ai" || "$domain" == *"workbuddy.ai"* ]]; then
        REGION_LABEL="Global (workbuddy.ai)"
    else
        REGION_LABEL="CN (codebuddy.cn)"
    fi

    step5_save_credentials
    show_summary
}

# ─── 主流程 ────────────────────────────────────────────────────────────────────

main() {
    print_banner

    # 检查依赖
    for cmd in curl python3; do
        if ! command -v "$cmd" &>/dev/null; then
            print_error "缺少依赖: $cmd"
            echo -e "  ${DIM}请先安装 $cmd 后再运行此脚本${NC}"
            exit 1
        fi
    done

    # 参数解析
    local region_arg="${1:-}"

    # 手动模式
    if [[ "$region_arg" == "manual" ]]; then
        manual_mode
        exit 0
    fi

    # 帮助
    if [[ "$region_arg" == "-h" || "$region_arg" == "--help" ]]; then
        echo "用法: $0 [cn|global|manual]"
        echo ""
        echo "  cn       登录 CN (codebuddy.cn)"
        echo "  global   登录 Global (workbuddy.ai)"
        echo "  manual   手动输入 Token"
        echo ""
        exit 0
    fi

    # 选择区域
    select_region "$region_arg"

    # 设置端点
    setup_endpoints

    echo -e "  ${DIM}区域: ${REGION_LABEL}${NC}"
    echo -e "  ${DIM}认证端点: ${AUTH_BASE}${NC}"
    echo ""

    # 执行登录流程
    step1_get_auth_state
    step2_open_browser
    step3_poll_token
    step4_get_account
    step5_save_credentials
    step6_verify
    show_summary
}

main "$@"
