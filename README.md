
# 针对校园网认证的自动认证方案

## 概述

本文档介绍一个基于 Python 实现的校园网自动认证登录脚本。该脚本通过检测网络状态、捕获认证参数、获取 OAuth 授权码并提交登录请求，实现无人工干预的网络接入。
本教程仅限使用于已购买套餐的用户，不存在任何绕过套餐认证脚本，未购买套餐的用户无法使用，请注意甄别。

## 技术栈

- **语言**: Python 3
- **核心依赖**: requests 库
- **网络协议**: HTTP/HTTPS
- **认证机制**: OAuth 2.0 + SSO 单点登录

## 工作流程

### 1. 网络连接状态检测

脚本首先检测当前网络连接状态：

```python
def check_network():
    try:
        r = requests.get(CHECK_URL, timeout=5, allow_redirects=False)
        return r.status_code == 204
    except Exception:
        return False
```

**检测机制:**
- 访问状态检测端点（通常返回 HTTP 204 表示网络正常）
- 返回 204 状态码表示网络已连接，脚本直接退出
- 否则进入重连和认证流程

### 2. 网络接口重置

当检测到网络断连时，重置 WAN 接口并等待新 IP 分配：

```python
def reconnect_wan():
    log("Reconnecting WAN...")
    subprocess.run(["ifdown", "wan"], timeout=10)
    time.sleep(5)
    subprocess.run(["ifup", "wan"], timeout=10)
    
    # 等待获取新 IP
    for _ in range(15):
        try:
            out = subprocess.run(["ifstatus", "wan"], 
                               capture_output=True, 
                               text=True, 
                               timeout=5).stdout
            if '"ipv4-address"' in out:
                log("WAN got new IP.")
                return True
        except Exception:
            pass
        time.sleep(2)
    return False
```

**操作说明:**
- 关闭并重新启动 WAN 接口
- 轮询检查接口状态，确认已获取新 IP 地址
- 最多等待 30 秒（15 次 × 2 秒）

### 3. 动态参数捕获

通过触发网关重定向获取认证所需参数：

```python
def get_param(text, key):
    m = re.search(rf'{key}=([^&"\'\s<>\\]+)', text)
    return m.group(1) if m else ""

# 捕获重定向响应
r = session.get(CHECK_URL, allow_redirects=False, timeout=10)
if "wlanuserip" in r.text:
    portal_content = r.text
else:
    loc = r.headers.get("Location", "")
    if loc and "wlanuserip" in loc:
        portal_content = loc
```

**工作流程:**
1. 向检测端点发起请求
2. 网关返回包含认证参数的响应（可能在响应体或 Location 头中）
3. 使用正则表达式提取各项参数

**关键参数说明:**
- `wlanuserip`: 客户端 IP 地址
- `wlanacname`: 无线接入控制器名称
- `nasip`: 网络接入服务器 IP
- `mac`: 客户端 MAC 地址
- `nasid`: NAS 标识符
- `vid`: VLAN ID
- `port`: 端口号
- `nasportid`: NAS 端口标识符

### 4. 认证令牌获取

向认证 API 提交用户凭据获取访问令牌：

```python
r = session.post(
    API_LOGIN,
    json={"phone": PHONE, "uid": PASSWORD, "captchaKey": ""},
    headers=BROWSER_HEADERS,
    timeout=10,
)
satoken = r.json().get("data", {}).get("token", "")
```

**请求特点:**
- 使用 POST 方法提交 JSON 格式凭据
- 模拟浏览器请求头，包含 User-Agent、Origin、Referer 等
- 服务器验证成功后返回包含 token 的 JSON 响应

**响应格式:**
```json
{
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
  }
}
```

### 5. OAuth 授权流程

使用令牌获取 OAuth 授权码：

```python
inner_url = (
    f"{GATEWAY}?wlanuserip={wlanuserip}&wlanacname={wlanacname}"
    f"&ssid=&nasip={nasip}&snmpagentip=&mac={mac}&t=wireless-v2"
    f"&url={url_param}&apmac=&nasid={nasid}&vid={vid}&port={port}&nasportid={nasportid}"
)
redirect_uri_encoded = urllib.parse.quote(inner_url.replace("?", "%3F"), safe="")

oauth_url = (
    f"{API_OAUTH}?response_type=code"
    f"&client_id=YOUR_CLIENT_ID"
    f"&redirect_uri={redirect_uri_encoded}"
    f"&serviceName={SERVICE_NAME}"
)

oauth_headers = dict(BROWSER_HEADERS)
oauth_headers["satoken"] = satoken

r2 = session.get(oauth_url, headers=oauth_headers, allow_redirects=False, timeout=15)
```

**OAuth 流程说明:**
1. 构建回调 URL（包含所有网络参数）
2. 对回调 URL 进行 URL 编码
3. 请求 OAuth 授权端点，携带令牌
4. 从响应中提取授权码（code）

**授权码提取:**
```python
code_url = r2.headers.get("Location", "")
code = ""
if code_url:
    code = get_param(code_url, "code")
else:
    # 从响应体中提取
    m = re.search(r'["\']?code["\']?\s*[:=]\s*["\']?([A-Za-z0-9]{20,})', r2.text)
    if m:
        code = m.group(1)
```

### 6. 提交最终登录请求

使用授权码和网络参数完成 SSO 登录：

```python
final_url = (
    f"{GATEWAY}?wlanuserip={wlanuserip}&wlanacname={wlanacname}"
    f"&ssid=&nasip={nasip}&snmpagentip=&mac={mac}&t=wireless-v2"
    f"&url={url_param}&apmac=&nasid={nasid}&vid={vid}&port={port}&nasportid={nasportid}"
    f"&code={code}"
    f"&serviceName={SERVICE_NAME}"
    f"&apartmentId={APARTMENT_ID}"
    f"&roomId={ROOM_ID}"
)

final_headers = {
    "User-Agent": BROWSER_HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}
if api_jsessionid:
    final_headers["Cookie"] = f"JSESSIONID={api_jsessionid}"

session.get(final_url, headers=final_headers, allow_redirects=True, timeout=20)
```

**关键要素:**
- 携带 OAuth 授权码
- 包含所有网络参数和房间信息
- 保持会话 Cookie（JSESSIONID）
- 跟随重定向完成登录

### 7. 验证登录状态

等待短暂时间后再次检测网络连接：

```python
time.sleep(3)
if check_network():
    log("SUCCESS! Network is up.")
    return 0

log("Verification failed.")
return 1
```

## 完整示例代码

```python
#!/usr/bin/env python3
import re
import subprocess
import sys
import time
import urllib.parse

try:
    import requests
except ImportError:
    print("ERROR: python3-requests not installed.")
    sys.exit(1)

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://broadband.example.com",
    "Referer": "https://broadband.example.com/",
    "sec-ch-ua": '"Google Chrome";v="120", "Chromium";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}

# 配置项 - 请根据实际情况修改
PHONE = "USER_PHONE"           # 手机号
PASSWORD = "USER_ID"           # 用户ID
API_LOGIN = "https://api.example.com/ac/auth/loginByPhoneAndUid"
API_OAUTH = "https://api.example.com/ac/auth/oauthRedirect"
GATEWAY = "http://10.10.16.101:8080/eportal/login_sso.jsp"
CHECK_URL = "http://connect.rom.miui.com/generate_204"
SERVICE_NAME = "chinaTelecom"
APARTMENT_ID = "YOUR_APARTMENT_ID"
ROOM_ID = "YOUR_ROOM_ID"


def log(msg):
    """日志输出"""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check_network():
    """检测网络连接状态"""
    try:
        r = requests.get(CHECK_URL, timeout=5, allow_redirects=False)
        return r.status_code == 204
    except Exception:
        return False


def reconnect_wan():
    """重置 WAN 接口并等待新 IP"""
    log("Reconnecting WAN...")
    subprocess.run(["ifdown", "wan"], timeout=10)
    time.sleep(5)
    subprocess.run(["ifup", "wan"], timeout=10)
    
    for _ in range(15):
        try:
            out = subprocess.run(
                ["ifstatus", "wan"], 
                capture_output=True, 
                text=True, 
                timeout=5
            ).stdout
            if '"ipv4-address"' in out:
                log("WAN got new IP.")
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def get_param(text, key):
    """从文本中提取指定参数值"""
    m = re.search(rf'{key}=([^&"\'\s<>\\]+)', text)
    return m.group(1) if m else ""


def main():
    # 1. 检测网络状态
    if check_network():
        return 0

    # 2. 重置网络接口
    if not reconnect_wan():
        log("Failed to reconnect WAN.")
        return 1

    # 3. 创建会话并设置请求头
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    # 4. 捕获认证参数
    portal_content = None
    try:
        r = session.get(CHECK_URL, allow_redirects=False, timeout=10)
        if "wlanuserip" in r.text:
            portal_content = r.text
        else:
            loc = r.headers.get("Location", "")
            if loc and "wlanuserip" in loc:
                portal_content = loc
    except Exception as e:
        log(f"Probe failed: {e}")

    if not portal_content:
        log("Could not capture portal parameters.")
        return 1

    # 5. 提取网络参数
    wlanuserip = get_param(portal_content, "wlanuserip")
    wlanacname = get_param(portal_content, "wlanacname")
    nasip = get_param(portal_content, "nasip")
    mac = get_param(portal_content, "mac")
    url_param = get_param(portal_content, "url")
    nasid = get_param(portal_content, "nasid")
    vid = get_param(portal_content, "vid")
    port = get_param(portal_content, "port")
    nasportid = get_param(portal_content, "nasportid")

    if not wlanuserip:
        log("Missing wlanuserip.")
        return 1

    # 6. 获取认证令牌
    r = session.post(
        API_LOGIN,
        json={"phone": PHONE, "uid": PASSWORD, "captchaKey": ""},
        headers=BROWSER_HEADERS,
        timeout=10,
    )
    satoken = r.json().get("data", {}).get("token", "")
    if not satoken:
        log(f"No token: {r.text[:200]}")
        return 1

    # 7. 构建 OAuth 请求
    inner_url = (
        f"{GATEWAY}?wlanuserip={wlanuserip}&wlanacname={wlanacname}"
        f"&ssid=&nasip={nasip}&snmpagentip=&mac={mac}&t=wireless-v2"
        f"&url={url_param}&apmac=&nasid={nasid}&vid={vid}&port={port}&nasportid={nasportid}"
    )
    redirect_uri_encoded = urllib.parse.quote(inner_url.replace("?", "%3F"), safe="")

    oauth_url = (
        f"{API_OAUTH}?response_type=code"
        f"&client_id=6d6bc6f3b5f04107a5fc1c62e39dd5f4"
        f"&redirect_uri={redirect_uri_encoded}"
        f"&serviceName={SERVICE_NAME}"
    )

    oauth_headers = dict(BROWSER_HEADERS)
    oauth_headers["satoken"] = satoken

    # 8. 获取 OAuth 授权码
    r2 = session.get(oauth_url, headers=oauth_headers, allow_redirects=False, timeout=15)

    api_jsessionid = None
    for c in session.cookies:
        if c.name == "JSESSIONID":
            api_jsessionid = c.value

    code_url = r2.headers.get("Location", "")
    code = ""
    if code_url:
        code = get_param(code_url, "code")
    else:
        m = re.search(r'["\']?code["\']?\s*[:=]\s*["\']?([A-Za-z0-9]{20,})', r2.text)
        if m:
            code = m.group(1)

    if not code:
        log("Could not extract code.")
        return 1

    # 9. 提交最终登录请求
    final_url = (
        f"{GATEWAY}?wlanuserip={wlanuserip}&wlanacname={wlanacname}"
        f"&ssid=&nasip={nasip}&snmpagentip=&mac={mac}&t=wireless-v2"
        f"&url={url_param}&apmac=&nasid={nasid}&vid={vid}&port={port}&nasportid={nasportid}"
        f"&code={code}"
        f"&serviceName={SERVICE_NAME}"
        f"&apartmentId={APARTMENT_ID}"
        f"&roomId={ROOM_ID}"
    )

    final_headers = {
        "User-Agent": BROWSER_HEADERS["User-Agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Upgrade-Insecure-Requests": "1",
    }
    if api_jsessionid:
        final_headers["Cookie"] = f"JSESSIONID={api_jsessionid}"

    session.get(final_url, headers=final_headers, allow_redirects=True, timeout=20)

    # 10. 验证登录状态
    time.sleep(3)
    if check_network():
        log("SUCCESS! Network is up.")
        return 0

    log("Verification failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

## 部署与使用

### 环境要求

```bash
# 安装依赖
pip3 install requests

# 或在 OpenWrt 等嵌入式系统上
opkg update
opkg install python3-requests
```

### 配置说明

在脚本中修改以下配置项：

```python
PHONE = "13800138000"              # 替换为实际手机号
PASSWORD = "USER_ID_HERE"          # 替换为实际身份证后8位
API_LOGIN = "https://api.example.com/..." 
SERVICE_NAME = "chinaTelecom"      # 根据运营商选择（见下表）
APARTMENT_ID = "1234567890"        # 替换为公寓 ID (获取方式如下)
ROOM_ID = "0987654321"             # 替换为房间 ID (获取方式如下)
```

**运营商 SERVICE_NAME 对照表：**

| 运营商  | SERVICE_NAME 值 |
| ---- | -------------- |
| 中国电信 | `chinaTelecom` |
| 中国联通 | `chinaUnicom`  |
| 中国移动 | `chinaMobile`  |

**重要提醒：使用脚本前必须完成以上所有配置项的填写，缺少任何一项都会导致脚本无法正常工作。**

### 获取 APARTMENT_ID 和 ROOM_ID

这两个参数需要通过浏览器开发者工具获取：

1. **触发登录界面**
   - 连接到校园网（未认证状态）
   - 打开浏览器访问任意网站，会自动重定向到登录页面

2. **打开开发者工具**
   - 按 `F12` 键打开浏览器开发者工具
   - 切换到 **Network**（网络）标签页
   - 勾选 **Preserve log**（保留日志）选项

3. **执行一次正常登录**
   - 在登录页面输入账号密码并完成登录
   - 观察开发者工具的网络请求列表

4. **查找登录请求**
   - 在请求列表中找到 `login_sso.jsp` 请求
   - 点击该请求查看详情

5. **提取参数**
   - 在右侧面板切换到 **Headers**（标头）选项卡
   - 找到 **Request URL**（请求 URL）
   - 从 URL 查询参数中复制 `apartmentId` 和 `roomId` 的值

**示例 URL：**
```
http://10.10.16.101:8080/eportal/login_sso.jsp?wlanuserip=...&apartmentId=1234567890123456789&roomId=9876543210987654321&...
```

从中提取：
- `APARTMENT_ID = "1234567890123456789"`
- `ROOM_ID = "9876543210987654321"`

### 运行方式

**手动运行:**
```bash
chmod +x autologin.py
python3 autologin.py
```

**使用 cron 定时执行:**
```bash
# 编辑 crontab
crontab -e

# 添加定时任务（每 5 分钟检查一次）
*/5 * * * * /usr/bin/python3 /root/autologin.py >> /var/log/autologin.log 2>&1
```

**OpenWrt 系统服务（推荐）:**
```bash
# 将脚本放置到 /root/autologin.py
# 在 /etc/rc.local 中添加：
python3 /root/autologin.py &
```

## 技术要点总结

1. **HTTP 重定向捕获**: 利用未认证访问触发的网关重定向获取动态参数
2. **OAuth 2.0 授权**: 使用标准 OAuth 流程获取授权码
3. **会话管理**: 通过 requests.Session 保持 Cookie 和请求头一致性
4. **参数编码**: 正确处理 URL 编码，特别是嵌套参数
5. **错误处理**: 多个关键步骤都有失败检测和日志记录
6. **浏览器模拟**: 完整的请求头模拟，包括 sec-ch-ua 等现代浏览器特征

## 注意事项

1. **安全性**: 脚本包含用户凭据，应设置适当的文件权限（如 `chmod 600`）
2. **合规性**: 确保自动登录行为符合校园网使用政策和相关规定
3. **维护性**: API 端点和参数可能随系统升级而变化，需定期检查更新
4. **适用范围**: 本方案适用于特定的认证系统，其他环境可能需要调整
5. **依赖检查**: 脚本启动时会检查 requests 库是否安装，未安装会提示错误

## 许可与免责声明

本文档仅供技术学习和交流使用。使用者应遵守所在网络的使用政策和法律法规，作者不对因使用本文档内容而产生的任何后果承担责任。
