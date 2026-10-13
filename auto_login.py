import json
import os
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
API_LOGIN = "https://api.215123.cn/ac/auth/loginByPhoneAndUid"
API_OAUTH = "https://api.215123.cn/ac/auth/oauthRedirect"
GATEWAY = "http://10.10.16.101:8080/eportal/login_sso.jsp"
CHECK_URL = "http://connect.rom.miui.com/generate_204"
SERVICE_NAME = "chinaTelecom"
APARTMENT_ID = "YOUR_APARTMENT_ID"
ROOM_ID = "YOUR_ROOM_ID"

# 网段漂移自愈：使用固定候选，避免每轮生成新设备身份。
WAN_INTERFACE = "wan"
EXPECTED_WAN_PREFIX = "10.111.97"
MAC_CANDIDATES = (
    "38:02:E3:08:70:A6", "38:02:E3:08:70:A7",
    "38:02:E3:08:70:A8", "38:02:E3:08:70:A9",
)
MAC_COOLDOWN_SECONDS = 30 * 60
MAC_DHCP_WAIT_SECONDS = 10
MAC_STATE_FILE = "/root/.autologin-mac-recovery.json"
SCRIPT_LOCK_FILE = "/tmp/autologin.lock"


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


def wan_status():
    """空地址数组或命令失败都不能视为已获取 IPv4。"""
    try:
        result = subprocess.run(["ifstatus", WAN_INTERFACE], capture_output=True,
                                text=True, timeout=5)
        return json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def wan_ip():
    for address in wan_status().get("ipv4-address", []):
        ip = address.get("address", "")
        if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", ip):
            return ip
    return ""


def expected_wan_ip(ip):
    return ip.startswith(EXPECTED_WAN_PREFIX.rstrip(".") + ".")


def gateway_reachable():
    """HTTP 响应证明可达；连接超时证明不可达，其他错误用 ping 判定。"""
    gateway = urllib.parse.urlsplit(GATEWAY)
    probe_url = urllib.parse.urlunsplit((gateway.scheme, gateway.netloc,
                                        "/eportal/index.jsp", "", ""))
    try:
        requests.get(probe_url, timeout=(3, 3), allow_redirects=False)
        return True
    except requests.exceptions.ConnectTimeout:
        return False
    except requests.exceptions.ReadTimeout:
        # TCP 已连接，只是门户响应慢，不是连接不可达。
        return True
    except requests.exceptions.RequestException:
        try:
            result = subprocess.run(["ping", "-c", "2", "-W", "2", gateway.hostname],
                                    capture_output=True, text=True, timeout=6)
            if result.returncode in (0, 1):
                return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
        return None


def run_command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)
    return result.stdout


def reconnect_wan(minimum_wait=0, restore_mac=None, device=None):
    """重置 WAN 接口并等待新 IP"""
    log("Reconnecting WAN...")
    run_command(["ifdown", WAN_INTERFACE])
    time.sleep(5)
    if restore_mac:
        run_command(["ip", "link", "set", "dev", device, "address", restore_mac])
    run_command(["ifup", WAN_INTERFACE])
    time.sleep(minimum_wait)
    for _ in range(15):
        if wan_ip():
            log("WAN got new IP.")
            return True
        time.sleep(2)
    return False


def load_mac_state():
    try:
        with open(MAC_STATE_FILE, encoding="utf-8") as stream:
            state = json.load(stream)
        if not isinstance(state, dict):
            raise ValueError("MAC 状态文件格式错误")
        return state
    except FileNotFoundError:
        return {}


def save_mac_state(state):
    temporary = MAC_STATE_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, MAC_STATE_FILE)


def original_wan_mac():
    """找到 netifd 管理的设备 MAC 选项，并记录实际 MAC 和旧选项。"""
    status = wan_status()
    device = status.get("device") or status.get("l3_device")
    if not device or not re.fullmatch(r"[\w.:-]+", device):
        raise ValueError("无法确定 WAN 设备")
    output = run_command(["ip", "link", "show", "dev", device])
    match = re.search(r"link/ether\s+([0-9a-fA-F:]{17})", output)
    if not match:
        raise ValueError("无法读取原始 WAN MAC")
    config = {}
    for line in run_command(["uci", "-q", "show", "network"]).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            config[key] = value.strip("'")
    key = "network.{}.macaddr".format(WAN_INTERFACE)
    for section, kind in config.items():
        if kind == "device" and config.get(section + ".name") == device:
            key = section + ".macaddr"
            break
    return {"device": device, "mac": match.group(1).upper(),
            "key": key, "value": config.get(key)}


def restore_wan_mac(state):
    original = state["original"]
    if original["value"] is None:
        config = run_command(["uci", "-q", "show", "network"])
        if any(line.startswith(original["key"] + "=") for line in config.splitlines()):
            run_command(["uci", "-q", "delete", original["key"]])
    else:
        run_command(["uci", "set", original["key"] + "=" + original["value"]])
    run_command(["uci", "commit", "network"])
    run_command(["ubus", "call", "network", "reload"])
    has_ip = reconnect_wan(MAC_DHCP_WAIT_SECONDS, original["mac"], original["device"])
    # 恢复身份不依赖 DHCP 是否正常；没有地址仍须结束轮换并保留冷却。
    actual = run_command(["ip", "link", "show", "dev", original["device"]])
    if not re.search(r"link/ether\s+" + re.escape(original["mac"]), actual, re.I):
        raise ValueError("原 MAC 回读校验失败")
    state["pending"] = False
    save_mac_state(state)
    log("已恢复原始 MAC: {}{}".format(original["mac"], "" if has_ip else "，WAN 暂无 IP"))


def restore_interrupted_rotation():
    try:
        state = load_mac_state()
        if state.get("pending"):
            log("发现上次中断的 MAC 轮换，先恢复原始 MAC")
            restore_wan_mac(state)
        return True
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        log("MAC 状态读取或恢复失败，停止本轮: {}".format(error))
        return False


def rotate_wan_mac():
    state = None
    success = False
    try:
        previous = load_mac_state()
        remaining = MAC_COOLDOWN_SECONDS - (time.time() - previous.get("last_attempt", 0))
        if remaining > 0:
            log("MAC 轮换处于冷却期，剩余 {} 秒，等待下次 cron".format(int(remaining)))
            return False
        original = original_wan_mac()
        candidates = list(dict.fromkeys(mac.upper() for mac in MAC_CANDIDATES))
        candidates = [mac for mac in candidates if mac != original["mac"]]
        if not candidates or any(not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", mac)
                                 or int(mac[:2], 16) & 1 for mac in candidates):
            raise ValueError("候选 MAC 必须是有效单播地址")
        new_state = {"last_attempt": time.time(), "original": original, "pending": True}
        save_mac_state(new_state)  # 写入成功后才允许改 MAC 或执行回滚。
        state = new_state
        log("认证网关不可达，启动 MAC 轮换自愈...")
        for mac in candidates:
            log("尝试 MAC: " + mac)
            run_command(["uci", "set", original["key"] + "=" + mac])
            run_command(["ubus", "call", "network", "reload"])
            if not reconnect_wan(MAC_DHCP_WAIT_SECONDS):
                log("该 MAC 未获取 WAN IP")
                continue
            ip = wan_ip()
            log("新 IP: " + ip)
            if expected_wan_ip(ip) and gateway_reachable() is True:
                run_command(["uci", "commit", "network"])
                state["pending"] = False
                save_mac_state(state)
                success = True
                log("MAC 轮换成功，网关可达")
                log("新 MAC 会占用校园网设备名额，请到校园网后台清理旧设备")
                return True
        log("所有候选 MAC 均失败，恢复原始 MAC，等待下次 cron")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        log("MAC 轮换失败: {}".format(error))
    finally:
        if state is not None and not success:
            try:
                restore_wan_mac(state)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                log("原始 MAC 恢复失败，保留回滚记录，下次优先恢复: {}".format(error))
    return False


def prepare_wan():
    ip = wan_ip()
    if ip and not expected_wan_ip(ip) and gateway_reachable() is False:
        return rotate_wan_mac()
    if not reconnect_wan():
        return False
    # 普通重拨也可能被 DHCP 分到错误网段，拿到地址后再判定一次。
    ip = wan_ip()
    if ip and not expected_wan_ip(ip) and gateway_reachable() is False:
        return rotate_wan_mac()
    return True


def get_param(text, key):
    """从文本中提取指定参数值"""
    m = re.search(rf'{key}=([^&"\'\s<>\\]+)', text)
    return m.group(1) if m else ""


def main():
    if not restore_interrupted_rotation():
        return 1
    # 1. 检测网络状态
    if check_network():
        return 0

    # 2. 重置网络接口
    if not prepare_wan():
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


def run_script():
    """OpenWrt 上互斥整个运行过程，避免 cron 同时重拨或轮换。"""
    import fcntl
    with open(SCRIPT_LOCK_FILE, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("已有自动登录任务运行，跳过本轮")
            return 0
        return main()


if __name__ == "__main__":
    sys.exit(run_script())
