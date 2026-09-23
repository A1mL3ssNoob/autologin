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