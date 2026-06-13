#!/usr/bin/env python3
"""广东科技学院（GDUST）课表抓取工具 🎓

专为 GDUST 学生设计，通过 WebVPN 代理访问校园门户 API 获取课表数据。
支持 CAS 统一认证登录 + ddddocr 自动验证码识别。

用法:
  # 首次配置
  python3 gdust_timetable.py --set-credentials
  python3 gdust_timetable.py --set-token <从浏览器抓的TOKEN>

  # 自动登录（需安装 ddddocr）
  python3 gdust_timetable.py --auto-login --all-weeks

  # 半自动登录（AI 辅助场景）
  python3 gdust_timetable.py --begin-captcha-login
  python3 gdust_timetable.py --submit-captcha <验证码>

  # 抓取课表
  python3 gdust_timetable.py                  # 当前周
  python3 gdust_timetable.py --week 5         # 指定周
  python3 gdust_timetable.py --all-weeks      # 全学期
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

try:
    import ddddocr
    HAS_DDDDOCR = True
except ImportError:
    HAS_DDDDOCR = False

# ──────────────────────────── 常量 & 路径 ────────────────────────────

TZ = ZoneInfo("Asia/Shanghai")
CAS_API = "https://cas.gdust.edu.cn/cas-api"
WEBVPN_BASE = "https://webvpn.gdust.edu.cn"
WEBVPN_PROXY = f"{WEBVPN_BASE}/https/77726476706e69737468656265737421e0f85388263c26577a1d9ab8d6502720f0fcc5"
PORTAL_WEBVPN = f"{WEBVPN_PROXY}/smart-admin-api"
PORTAL_DIRECT = "https://portal.gdust.edu.cn/smart-admin-api"
VPN_QUERY = "vpn-12-o2-portal.gdust.edu.cn"

CONFIG_SEARCH_PATHS = [
    Path("config.json"),
    Path.home() / ".config" / "gdust-timetable" / "config.json",
]
SECRET_PATH = Path.home() / ".config" / "gdust-timetable" / "secrets.json"
DEFAULT_OUTPUT = Path("timetable.json")
CAPTCHA_PATH = Path("captcha.jpg")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
)

DEFAULT_VPN_COOKIES = {
    "wengine_vpn_ticketwebvpn_gdust_edu_cn": "wrdvpn1-19b4cf201d4c44799039165a5a64f1f4",
    "show_vpn": "1",
    "show_fast": "1",
    "heartbeat": "1",
    "show_faq": "0",
    "refresh": "0",
}


# ──────────────────────────── 配置加载 ────────────────────────────


def load_config() -> dict[str, Any]:
    """按优先级搜索 config.json，返回合并结果。"""
    for p in CONFIG_SEARCH_PATHS:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    print(
        "⚠️  未找到 config.json，请复制 config.example.json 并填写学号等信息：\n"
        "   cp config.example.json config.json\n"
        "   然后编辑 config.json",
        file=sys.stderr,
    )
    sys.exit(1)


def cfg(config: dict, key: str, fallback: Any = None) -> Any:
    """从 config 读取，支持 key 和嵌套 key。"""
    return config.get(key, fallback)


def get_network_mode(config: dict[str, Any]) -> str:
    """返回网络模式：'campus'（校内直连）或 'webvpn'（校外代理）。"""
    return config.get("network_mode", "webvpn")


def get_portal_api(config: dict[str, Any]) -> str:
    """根据网络模式返回门户 API 地址。"""
    if get_network_mode(config) == "campus":
        return PORTAL_DIRECT
    return PORTAL_WEBVPN


# ──────────────────────────── Secret 管理 ────────────────────────────


def load_secret() -> dict[str, Any]:
    if not SECRET_PATH.exists():
        return {}
    return json.loads(SECRET_PATH.read_text(encoding="utf-8"))


def save_secret(data: dict[str, Any]) -> None:
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SECRET_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(SECRET_PATH)
    os.chmod(SECRET_PATH, stat.S_IRUSR | stat.S_IWUSR)


def masked(s: str | None) -> str:
    if not s:
        return "<empty>"
    return s[:10] + "..." + s[-8:] if len(s) > 24 else "***"


# ──────────────────────────── 会话构造 ────────────────────────────


def make_session(secret: dict[str, Any], config: dict[str, Any]) -> requests.Session:
    """构造 session，根据网络模式设置不同的 headers 和 cookies。"""
    s = requests.Session()
    ua = config.get("user_agent") or secret.get("user_agent") or DEFAULT_USER_AGENT
    mode = get_network_mode(config)

    if mode == "campus":
        # 校内直连：直接访问 portal.gdust.edu.cn
        s.headers.update({
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://portal.gdust.edu.cn/",
            "DNT": "1",
        })
    else:
        # 校外 WebVPN：通过代理访问
        s.headers.update({
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{WEBVPN_PROXY}/",
            "DNT": "1",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        })
        cookies = secret.get("vpn_cookies") or DEFAULT_VPN_COOKIES
        for k, v in cookies.items():
            s.cookies.set(k, v, domain="webvpn.gdust.edu.cn")
    return s


def make_cas_session(secret: dict[str, Any], config: dict[str, Any]) -> requests.Session:
    """构造 CAS 登录用 session。"""
    s = requests.Session()
    ua = config.get("user_agent") or secret.get("user_agent") or DEFAULT_USER_AGENT
    s.headers.update({
        "User-Agent": ua,
        "Referer": "https://cas.gdust.edu.cn/cas/?service=https%3A%2F%2Fwebvpn.gdust.edu.cn%2F",
        "Origin": "https://cas.gdust.edu.cn",
    })
    return s


# ──────────────────────────── 学期计算 ────────────────────────────


def current_teaching_week(config: dict[str, Any]) -> int:
    """根据 config 中的 week1_monday 计算当前教学周。"""
    week1_str = config.get("week1_monday")
    if not week1_str:
        print("⚠️  config.json 缺少 week1_monday 字段", file=sys.stderr)
        sys.exit(1)
    week1 = datetime.strptime(week1_str, "%Y-%m-%d").replace(tzinfo=TZ).date()
    today = datetime.now(TZ).date()
    return max(1, min(20, ((today - week1).days // 7) + 1))


# ──────────────────────────── API 调用 ────────────────────────────


def fetch_week(s: requests.Session, token: str, config: dict[str, Any], week: int, job_number: str | None = None) -> tuple[bool, dict | None, str]:
    """请求指定周的课表数据。"""
    secret = load_secret()
    if not job_number:
        job_number = secret.get("username") or config.get("job_number")
    year = config.get("year")
    semester = config.get("semester")
    portal_api = get_portal_api(config)
    mode = get_network_mode(config)
    base_url = f"{portal_api}/app/zf/get_student_course"

    if mode == "campus":
        url = f"{base_url}?jobNumber={job_number}&year={year}&semester={semester}&week={week}"
    else:
        url = f"{base_url}?{VPN_QUERY}&jobNumber={job_number}&year={year}&semester={semester}&week={week}"

    resp = s.get(url, headers={"TOKEN": token}, timeout=25)
    try:
        data = resp.json()
    except Exception:
        return False, None, f"HTTP {resp.status_code}: non-json response"
    if data.get("success"):
        return True, data, "success"
    return False, data, str(data.get("msg") or data.get("message") or data)[:300]


def token_works(s: requests.Session, token: str, config: dict[str, Any], week: int | None = None) -> bool:
    """测试 TOKEN 是否仍然有效。"""
    ok, _data, _msg = fetch_week(s, token, config, week or current_teaching_week(config))
    return ok


# ──────────────────────────── 验证码 & 登录 ────────────────────────────


def write_captcha(data_url: str) -> Path:
    """将 base64 验证码图片写入本地文件。"""
    m = re.match(r"data:image/[^;]+;base64,(.+)", data_url or "")
    if not m:
        raise RuntimeError("CAS 未返回验证码图片")
    CAPTCHA_PATH.write_bytes(base64.b64decode(m.group(1)))
    return CAPTCHA_PATH


def require_credentials(secret: dict[str, Any]) -> tuple[str, str]:
    """获取校园账号密码，未配置则报错。"""
    username = secret.get("username")
    password = secret.get("password")
    if not username or not password:
        raise RuntimeError(
            "缺少校园账号/密码。请先运行：\n"
            "  python3 gdust_timetable.py --set-credentials"
        )
    return username, password


def begin_captcha_login(secret: dict[str, Any], config: dict[str, Any]) -> Path:
    """获取 CAS 验证码图片，保存到本地，等待用户输入。"""
    require_credentials(secret)
    s = make_cas_session(secret, config)
    code_resp = s.get(f"{CAS_API}/cas/loginCode", timeout=20)
    code_resp.raise_for_status()
    code_json = code_resp.json()
    if code_json.get("code") != 0:
        raise RuntimeError(f"获取验证码失败: {code_json}")
    captcha_path = write_captcha(code_json["data"]["codeUrl"])
    pending = {
        "uuid": code_json["data"]["uuid"],
        "created_at": int(time.time()),
    }
    new_secret = dict(secret)
    new_secret["pending_captcha"] = pending
    new_secret["user_agent"] = secret.get("user_agent") or DEFAULT_USER_AGENT
    save_secret(new_secret)
    print(f"✅ 验证码已保存: {captcha_path}")
    return captcha_path


def exchange_castgc_for_token(secret: dict[str, Any], config: dict[str, Any], castgc: str) -> str:
    """将 CAS TGC 换取 portal TOKEN。"""
    mode = get_network_mode(config)
    portal_api = get_portal_api(config)

    if mode == "webvpn":
        # WebVPN 模式：需要先获取 VPN cookies
        _init = requests.Session()
        _init.headers.update({"User-Agent": config.get("user_agent") or DEFAULT_USER_AGENT})
        _init.get(f"{WEBVPN_BASE}/", allow_redirects=True)
        fresh_vpn = {}
        for c in _init.cookies:
            if "webvpn" in c.domain:
                fresh_vpn[c.name] = c.value
        if fresh_vpn:
            secret = dict(secret)
            old = secret.get("vpn_cookies") or {}
            old.update(fresh_vpn)
            secret["vpn_cookies"] = old
            save_secret(secret)

    portal_s = make_session(secret, config)
    if mode == "campus":
        portal_resp = portal_s.get(
            f"{portal_api}/user/login",
            params={"loginCode": castgc, "appId": "portalRemote"},
            timeout=20,
        )
    else:
        portal_resp = portal_s.get(
            f"{portal_api}/user/login",
            params={VPN_QUERY: "", "loginCode": castgc, "appId": "portalRemote"},
            timeout=20,
        )
    portal_resp.raise_for_status()
    try:
        portal_json = portal_resp.json()
    except Exception:
        print(f"Portal 响应非 JSON: {portal_resp.text[:500]}", file=sys.stderr)
        raise RuntimeError("portal API 返回非 JSON 响应，可能是 VPN/代理问题")

    token = None
    for path in (("data", "data", "userBase", "token"), ("data", "userBase", "token")):
        cur = portal_json
        try:
            for key in path:
                cur = cur[key]
            token = cur
            break
        except Exception:
            pass
    if not token:
        raise RuntimeError(f"portal TOKEN 交换失败: {portal_json}")
    return token


def submit_captcha(secret: dict[str, Any], config: dict[str, Any], captcha: str) -> str:
    """提交验证码，完成登录，刷新 TOKEN。"""
    username, password = require_credentials(secret)
    pending = secret.get("pending_captcha") or {}
    uuid_val = pending.get("uuid")
    if not uuid_val:
        raise RuntimeError("没有待提交的验证码，请先运行 --begin-captcha-login")
    if int(time.time()) - int(pending.get("created_at", 0)) > 600:
        raise RuntimeError("验证码可能已过期，请重新运行 --begin-captcha-login")

    s = make_cas_session(secret, config)
    payload = {"loginName": username, "loginPwd": password, "code": captcha, "uuid": uuid_val}
    login_resp = s.post(f"{CAS_API}/cas/loginByAccount", json=payload, timeout=20)
    login_resp.raise_for_status()
    login_json = login_resp.json()
    if login_json.get("code") != 0 or not login_json.get("data"):
        raise RuntimeError(f"CAS 登录失败: {login_json.get('msg') or login_json}")
    castgc = login_json["data"]
    token = exchange_castgc_for_token(secret, config, castgc)

    new_secret = dict(secret)
    new_secret.update({"portal_token": token, "castgc": castgc, "updated_at": int(time.time())})
    new_secret.pop("pending_captcha", None)
    save_secret(new_secret)
    print(f"✅ TOKEN 已刷新: {masked(token)}")
    return token


def auto_login(secret: dict[str, Any], config: dict[str, Any], max_retries: int = 3) -> str:
    """全自动登录：OCR 识别验证码 + 提交，失败自动重试。"""
    if not HAS_DDDDOCR:
        raise RuntimeError("ddddocr 未安装，请先: pip3 install ddddocr")
    require_credentials(secret)
    ocr = ddddocr.DdddOcr(show_ad=False)
    for attempt in range(1, max_retries + 1):
        print(f"🔄 自动登录尝试 {attempt}/{max_retries}...")
        path = begin_captcha_login(secret, config)
        img_bytes = path.read_bytes()
        captcha_text = ocr.classification(img_bytes).strip()
        if not captcha_text:
            print(f"⚠️  OCR 识别为空，重试...")
            time.sleep(1)
            continue
        print(f"🔍 OCR 识别: {captcha_text}")
        try:
            secret = load_secret()
            token = submit_captcha(secret, config, captcha_text)
            return token
        except RuntimeError as e:
            print(f"❌ 登录失败: {e}")
            if attempt < max_retries:
                time.sleep(1)
    raise RuntimeError(f"自动登录 {max_retries} 次均失败，请手动 --begin-captcha-login")


def refresh_token_interactive(secret: dict[str, Any], config: dict[str, Any]) -> str:
    """交互式登录：手动输入验证码。"""
    if not secret.get("username"):
        secret["username"] = input("校园账号: ").strip()
    if not secret.get("password"):
        secret["password"] = getpass.getpass("校园密码: ")
    save_secret(secret)
    path = begin_captcha_login(secret, config)
    print(f"验证码已保存到: {path}")
    captcha = input("请输入验证码: ").strip()
    return submit_captcha(load_secret(), config, captcha)


# ──────────────────────────── 课表抓取 & 输出 ────────────────────────────


def load_existing_timetable(output: Path) -> dict[str, Any]:
    if not output.exists():
        return {}
    try:
        data = json.loads(output.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_weeks(output: Path, week_data: dict[str, Any]) -> None:
    """将新数据合并写入输出文件。"""
    merged = load_existing_timetable(output)
    merged.update(week_data)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_weeks(
    token: str, secret: dict[str, Any], config: dict[str, Any],
    weeks: list[int], output: Path,
) -> bool:
    """批量抓取多个教学周的课表。"""
    s = make_session(secret, config)
    week_label = "、".join(str(w) for w in weeks)
    print(f"🚀 正在同步第 {week_label} 周课表...")
    week_data: dict[str, Any] = {}
    failures: list[str] = []
    for w in weeks:
        ok, data, msg = fetch_week(s, token, config, w)
        if ok and data:
            week_data[str(w)] = data["data"]["courseList"]
            print(f"  ✅ 第 {w} 周成功")
        else:
            print(f"  ❌ 第 {w} 周失败: {msg}")
            failures.append(f"week {w}: {msg}")
        time.sleep(0.3)  # 礼貌间隔

    if week_data:
        write_weeks(output, week_data)
        total = sum(len(v) for v in week_data.values())
        print(f"\n🎉 完成！共 {total} 条课程记录 → {output}")
    else:
        print("\n⚠️  本次未拉到任何数据，未覆盖现有文件。")
    return bool(week_data) and not failures


# ──────────────────────────── CLI ────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description="广东科技学院（GDUST）课表抓取工具 🎓",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--week", type=int, help="抓取指定教学周（1-20）")
    ap.add_argument("--all-weeks", action="store_true", help="抓取全学期 20 周")
    ap.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="输出文件路径（默认 timetable.json）")

    login_group = ap.add_argument_group("登录相关")
    login_group.add_argument("--set-credentials", action="store_true", help="设置校园账号密码")
    login_group.add_argument("--set-token", metavar="TOKEN", help="手动设置 portal TOKEN（从浏览器抓）")
    login_group.add_argument("--set-vpn-cookies", metavar="JSON", help="手动设置 VPN cookies")
    login_group.add_argument("--auto-login", action="store_true", help="全自动登录（需 ddddocr）")
    login_group.add_argument("--refresh-login", action="store_true", help="交互式登录")
    login_group.add_argument("--begin-captcha-login", action="store_true", help="获取验证码图片")
    login_group.add_argument("--submit-captcha", metavar="CODE", help="提交验证码")

    args = ap.parse_args()
    config = load_config()
    secret = load_secret()

    # ── 设置类操作 ──
    if args.set_credentials:
        secret["username"] = input("校园账号: ").strip()
        secret["password"] = getpass.getpass("校园密码: ")
        secret["user_agent"] = secret.get("user_agent") or config.get("user_agent") or DEFAULT_USER_AGENT
        save_secret(secret)
        print(f"✅ 已保存到 {SECRET_PATH}（权限 0600）")
        return 0

    if args.set_token:
        secret["portal_token"] = args.set_token.strip()
        secret["updated_at"] = int(time.time())
        save_secret(secret)
        print(f"✅ TOKEN 已设置: {masked(secret['portal_token'])}")
        return 0

    if args.set_vpn_cookies:
        try:
            cookies = json.loads(args.set_vpn_cookies)
            secret["vpn_cookies"] = cookies
            save_secret(secret)
            print(f"✅ VPN cookies 已设置: {list(cookies.keys())}")
        except json.JSONDecodeError as e:
            print(f"❌ JSON 解析失败: {e}", file=sys.stderr)
            return 1
        return 0

    if args.begin_captcha_login:
        begin_captcha_login(secret, config)
        return 0

    if args.submit_captcha:
        token = submit_captcha(secret, config, args.submit_captcha.strip())
        weeks = list(range(1, 21)) if args.all_weeks else [args.week or current_teaching_week(config)]
        ok = fetch_weeks(token, load_secret(), config, weeks, args.output)
        return 0 if ok else 2

    # ── 抓取流程 ──
    token = secret.get("portal_token")
    s = make_session(secret, config)

    if args.auto_login:
        token = auto_login(secret, config)
    elif args.refresh_login:
        token = refresh_token_interactive(secret, config)
    elif not token:
        print("❌ 无 TOKEN，请先运行 --set-credentials 和以下任一方式：", file=sys.stderr)
        print("   --auto-login     自动登录（需 ddddocr）", file=sys.stderr)
        print("   --refresh-login  交互式登录", file=sys.stderr)
        print("   --set-token      手动粘贴 TOKEN", file=sys.stderr)
        return 3
    elif not token_works(s, token, config, args.week or current_teaching_week(config)):
        print("⚠️  TOKEN 已失效，尝试自动登录...", file=sys.stderr)
        if HAS_DDDDOCR:
            token = auto_login(secret, config)
        else:
            print("❌ ddddocr 未安装，无法自动刷新。请手动：", file=sys.stderr)
            print("   python3 gdust_timetable.py --refresh-login", file=sys.stderr)
            return 3

    weeks = list(range(1, 21)) if args.all_weeks else [args.week or current_teaching_week(config)]
    ok = fetch_weeks(token, load_secret(), config, weeks, args.output)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
