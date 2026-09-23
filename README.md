# 校园网自动登录脚本

## 概述

本项目提供一个基于 Python 实现的校园网自动认证登录脚本。该脚本通过检测网络状态、捕获认证参数、获取 OAuth 授权码并提交登录请求，实现无人工干预的网络接入。

**重要说明**：
- 本项目仅适用于已购买校园网套餐的用户，不存在任何绕过套餐认证的功能。未购买套餐的用户无法使用，请注意甄别。
- 你所填写的敏感信息不会泄露给第三方，运营商更不会根据你填写的房间和楼层号找上门，因为正常请求也会发送这些信息。

## 环境配置

### 1. 环境要求

**Python 版本：**
- Python 3.6 或更高版本

**必需依赖：**
- `requests` 库（用于 HTTP 请求）

### 2. 检查 Python 版本

```bash
python3 --version
```

如果输出类似 `Python 3.x.x`，则表示已安装 Python 3。

### 3. 安装依赖

**在常规 Linux 系统上：**

```bash
# 使用 pip 安装
pip3 install requests

# 或使用系统包管理器（Debian/Ubuntu）
sudo apt update
sudo apt install python3-requests
```

**在 OpenWrt 路由器系统上：**

```bash
# 更新软件包列表
opkg update

# 安装 Python 3（如未安装）
opkg install python3

# 安装 pip
opkg install python3-pip

# 安装 requests 库
pip3 install requests
```

**注意**：OpenWrt 系统存储空间有限，安装前请确保有足够空间（建议至少 20MB 可用空间）。

### 4. 验证安装

```bash
python3 -c "import requests; print('requests 库安装成功')"
```

如果输出 `requests 库安装成功`，说明环境配置完成。

## 配置说明

### 必填配置项

在 `auto_login.py` 脚本中修改以下配置：

```python
PHONE = "13800138000"              # 替换为实际手机号
PASSWORD = "USER_ID_HERE"          # 替换为实际用户 ID（通常是身份证后8位）
API_LOGIN = "https://api.example.com/ac/auth/loginByPhoneAndUid"  # 替换为实际 API 地址
API_OAUTH = "https://api.example.com/ac/auth/oauthRedirect"        # 替换为实际 OAuth 地址
SERVICE_NAME = "chinaTelecom"      # 根据运营商选择（见下表）
APARTMENT_ID = "1234567890"        # 替换为实际公寓 ID（获取方式见下文）
ROOM_ID = "0987654321"             # 替换为实际房间 ID（获取方式见下文）
```

### 运营商配置对照表

| 运营商 | SERVICE_NAME 值 |
|--------|-----------------|
| 中国电信 | `chinaTelecom` |
| 中国联通 | `chinaUnicom` |
| 中国移动 | `chinaMobile` |

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

**重要提醒**：使用脚本前必须完成以上所有配置项的填写，缺少任何一项都会导致脚本无法正常工作。

## 使用方法

### 手动运行

```bash
chmod +x auto_login.py
python3 auto_login.py
```

### 使用 cron 定时执行

```bash
# 编辑 crontab
crontab -e

# 添加定时任务（每 5 分钟检查一次）
*/5 * * * * /usr/bin/python3 /path/to/auto_login.py >> /var/log/autologin.log 2>&1
```

### OpenWrt 系统服务（推荐）

```bash
# 将脚本放置到 /root/auto_login.py
# 在 /etc/rc.local 中添加（在 exit 0 之前）：
python3 /root/auto_login.py &
```

## 工作原理

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

**工作流程：**
1. 向检测端点发起请求
2. 网关返回包含认证参数的响应（可能在响应体或 Location 头中）
3. 使用正则表达式提取各项参数

**关键参数说明：**
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

- 使用 POST 方法提交 JSON 格式凭据
- 模拟浏览器请求头，包含 User-Agent、Origin、Referer 等
- 服务器验证成功后返回包含 token 的 JSON 响应

### 5. OAuth 授权流程

使用令牌获取 OAuth 授权码：

```python
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

**OAuth 流程说明：**
1. 构建回调 URL（包含所有网络参数）
2. 对回调 URL 进行 URL 编码
3. 请求 OAuth 授权端点，携带令牌
4. 从响应中提取授权码（code）

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

session.get(final_url, headers=final_headers, allow_redirects=True, timeout=20)
```

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

## 技术要点

1. **HTTP 重定向捕获**：利用未认证访问触发的网关重定向获取动态参数
2. **OAuth 2.0 授权**：使用标准 OAuth 流程获取授权码
3. **会话管理**：通过 requests.Session 保持 Cookie 和请求头一致性
4. **参数编码**：正确处理 URL 编码，特别是嵌套参数
5. **错误处理**：多个关键步骤都有失败检测和日志记录
6. **浏览器模拟**：完整的请求头模拟，包括 sec-ch-ua 等现代浏览器特征

## 注意事项

1. **安全性**：脚本包含用户凭据，应设置适当的文件权限（如 `chmod 600`）
2. **合规性**：确保自动登录行为符合校园网使用政策和相关规定
3. **维护性**：API 端点和参数可能随系统升级而变化，需定期检查更新
4. **适用范围**：本方案适用于特定的认证系统，其他环境可能需要调整
5. **依赖检查**：脚本启动时会检查 requests 库是否安装，未安装会提示错误

## 许可与免责声明

本项目仅供技术学习和交流使用。使用者应遵守所在网络的使用政策和法律法规，作者不对因使用本项目内容而产生的任何后果承担责任。
