#!/usr/bin/env python3
"""GDUST 课表抓取工具 — Web GUI

启动方式：
  python3 app.py

浏览器打开 http://localhost:5000
"""

from __future__ import annotations

import json
import os
import sys
import time
import threading
import uuid
from pathlib import Path
from threading import Lock

from flask import Flask, jsonify, render_template, request, send_file, url_for

# 将当前目录加入 path，确保能 import 主脚本
sys.path.insert(0, str(Path(__file__).parent))
from gdust_timetable import (
    CAPTCHA_PATH,
    CAS_API,
    DEFAULT_OUTPUT,
    DEFAULT_USER_AGENT,
    WEBVPN_BASE,
    fetch_week,
    load_config,
    load_secret,
    make_session,
    save_secret,
    submit_captcha,
    begin_captcha_login,
    auto_login,
    exchange_castgc_for_token,
    current_teaching_week,
    token_works,
    load_existing_timetable,
    write_weeks,
    make_cas_session,
)

app = Flask(__name__)

# 全局锁：防止并发请求同时操作 TOKEN / 抓取
_operation_lock = Lock()

# SSE 扫码登录会话管理
_scan_sessions: dict[str, dict] = {}
_scan_lock = Lock()


# ──────────────────────────── 辅助函数 ────────────────────────────


def _status() -> dict:
    """返回当前状态摘要。"""
    config = load_config()
    secret = load_secret()
    has_token = bool(secret.get("portal_token"))
    has_creds = bool(secret.get("username") and secret.get("password"))
    try:
        week = current_teaching_week(config)
    except Exception:
        week = None
    return {
        "has_credentials": has_creds,
        "has_token": has_token,
        "current_week": week,
        "year": config.get("year", ""),
        "semester": config.get("semester", ""),
        "week1_monday": config.get("week1_monday", ""),
        "network_mode": config.get("network_mode", "webvpn"),
    }


def _check_token() -> tuple[bool, str]:
    """检查 TOKEN 是否可用。返回 (ok, message)。"""
    config = load_config()
    secret = load_secret()
    token = secret.get("portal_token")
    if not token:
        return False, "无 TOKEN，请先登录"
    s = make_session(secret, config)
    try:
        if token_works(s, token, config):
            return True, "TOKEN 有效"
        return False, "TOKEN 已失效，请重新登录"
    except Exception as e:
        return False, f"检查失败: {e}"


def _subscribe_sse(session_id: str, config: dict, secret: dict):
    """后台线程：连接 CAS SSE，监听扫码状态。"""
    import requests as sse_requests
    mode = config.get("network_mode", "webvpn")
    if mode == "webvpn":
        # WebVPN 模式：需要先拿到 VPN cookies
        vpn_s = sse_requests.Session()
        vpn_s.headers.update({"User-Agent": DEFAULT_USER_AGENT})
        vpn_s.get(f"{WEBVPN_BASE}/", allow_redirects=True, timeout=15)
        vpn_cookies = {}
        for c in vpn_s.cookies:
            if "webvpn" in c.domain:
                vpn_cookies[c.name] = c.value
        cas_base = f"{WEBVPN_BASE}/https/6361732e67647573742e6564752e636e"
    else:
        cas_base = "https://cas.gdust.edu.cn"
        vpn_cookies = {}

    s = sse_requests.Session()
    s.headers.update({
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": f"{cas_base}/cas/",
        "Origin": cas_base,
    })
    if vpn_cookies:
        for k, v in vpn_cookies.items():
            s.cookies.set(k, v)
    try:
        sse_url = f"{cas_base}/cas-api/sse/subscribe"
        resp = s.get(sse_url, stream=True, timeout=120)
        resp.raise_for_status()
        print(f"[SSE] Connected to {sse_url}, status={resp.status_code}", flush=True)
        current_event = None
        data_buffer: list[str] = []
        for line in resp.iter_lines(decode_unicode=True):
            if line is None:
                continue
            # 打印每一行，方便调试
            print(f"[SSE RAW] {line!r}", flush=True)
            if line == "" or line.startswith(":"):
                # 空行 = 事件结束，处理积攒的 data
                if data_buffer and current_event:
                    data_str = "\n".join(data_buffer)
                    print(f"[SSE EVENT] event={current_event!r} data={data_str[:200]!r}", flush=True)
                    _handle_sse_event(session_id, current_event, data_str, secret, config)
                data_buffer = []
                current_event = None
                continue
            if line.startswith("event:"):
                current_event = line[6:].strip()
            elif line.startswith("data:"):
                data_buffer.append(line[5:].strip())
            elif ":" in line:
                key, value = line.split(":", 1)
                if key.strip().lower() == "event":
                    current_event = value.strip()
                elif key.strip().lower() == "data":
                    data_buffer.append(value.strip())
    except Exception as e:
        with _scan_lock:
            if session_id in _scan_sessions:
                _scan_sessions[session_id]["status"] = "error"
                _scan_sessions[session_id]["error"] = str(e)


def _handle_sse_event(session_id: str, evt: str, data: str, secret: dict, config: dict):
    """处理单个 SSE 事件。"""
    if evt == "HELLO":
        with _scan_lock:
            if session_id in _scan_sessions:
                _scan_sessions[session_id]["client_id"] = data
                _scan_sessions[session_id]["status"] = "waiting_scan"
    elif evt == "SUCCESS":
        with _scan_lock:
            if session_id in _scan_sessions:
                _scan_sessions[session_id]["status"] = "scanned"
    elif evt == "LOGIN_SUCCESS_TOKEN":
        with _scan_lock:
            if session_id in _scan_sessions:
                _scan_sessions[session_id]["status"] = "confirming"
    elif evt == "LOGIN_SUCCESS_TICKET":
        # 拿到 CASTGC，先换 TOKEN 再设状态（避免竞态）
        print(f"[SSE] LOGIN_SUCCESS_TICKET, data length={len(data)}", flush=True)
        tgc = data
        token = None
        token_error = None
        with _operation_lock:
            try:
                token = exchange_castgc_for_token(secret, config, tgc)
                print(f"[SSE] Token exchange OK", flush=True)
            except Exception as e:
                token_error = str(e)
                print(f"[SSE] Token exchange failed: {e}", flush=True)
        # token 换完再设 success
        with _scan_lock:
            if session_id in _scan_sessions:
                _scan_sessions[session_id]["status"] = "success"
                _scan_sessions[session_id]["tgc"] = tgc
                if token:
                    _scan_sessions[session_id]["token"] = token
                if token_error:
                    _scan_sessions[session_id]["token_error"] = token_error
    elif evt == "NEED_TO_BIND":
        with _scan_lock:
            if session_id in _scan_sessions:
                _scan_sessions[session_id]["status"] = "need_bind"
                _scan_sessions[session_id]["union_id"] = data


# ──────────────────────────── 页面路由 ────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


# ──────────────────────────── API 路由 ────────────────────────────


@app.route("/api/status")
def api_status():
    """获取当前状态。"""
    s = _status()
    token_ok, token_msg = _check_token() if s["has_token"] else (False, "无 TOKEN")
    s["token_ok"] = token_ok
    s["token_msg"] = token_msg
    return jsonify(s)


@app.route("/api/save-config", methods=["POST"])
def api_save_config():
    """保存配置信息（年份、学期、起始日、网络模式）。"""
    data = request.json
    config_path = Path("config.json")
    config = {}
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))

    for key in ("year", "semester", "week1_monday", "network_mode"):
        if key in data and data[key]:
            config[key] = data[key]

    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return jsonify({"ok": True, "message": "配置已保存"})


@app.route("/api/save-credentials", methods=["POST"])
def api_save_credentials():
    """保存校园账号密码。"""
    data = request.json
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"ok": False, "message": "账号和密码不能为空"})

    secret = load_secret()
    secret["username"] = username
    secret["password"] = password
    save_secret(secret)
    return jsonify({"ok": True, "message": "账号密码已保存"})


@app.route("/api/begin-captcha", methods=["POST"])
def api_begin_captcha():
    """获取验证码图片。"""
    with _operation_lock:
        try:
            config = load_config()
            secret = load_secret()
            path = begin_captcha_login(secret, config)
            return jsonify({"ok": True, "message": "验证码已生成", "path": str(path)})
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)})


@app.route("/api/captcha-image")
def api_captcha_image():
    """返回验证码图片。"""
    if CAPTCHA_PATH.exists():
        return send_file(CAPTCHA_PATH, mimetype="image/jpeg")
    return "No captcha", 404


@app.route("/api/submit-captcha", methods=["POST"])
def api_submit_captcha():
    """提交验证码，完成登录。"""
    with _operation_lock:
        code = request.json.get("code", "").strip()
        if not code:
            return jsonify({"ok": False, "message": "验证码不能为空"})
        try:
            secret = load_secret()
            config = load_config()
            token = submit_captcha(secret, config, code)
            return jsonify({"ok": True, "message": f"登录成功！TOKEN: {token[:16]}..."})
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)})


@app.route("/api/auto-login", methods=["POST"])
def api_auto_login():
    """全自动登录（OCR 验证码）。"""
    with _operation_lock:
        try:
            config = load_config()
            secret = load_secret()
            token = auto_login(secret, config)
            return jsonify({"ok": True, "message": f"自动登录成功！"})
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)})


@app.route("/api/set-token", methods=["POST"])
def api_set_token():
    """手动设置 TOKEN。"""
    token = request.json.get("token", "").strip()
    if not token:
        return jsonify({"ok": False, "message": "TOKEN 不能为空"})
    secret = load_secret()
    secret["portal_token"] = token
    secret["updated_at"] = int(time.time())
    save_secret(secret)
    return jsonify({"ok": True, "message": "TOKEN 已设置"})


@app.route("/api/wechat-login", methods=["POST"])
def api_wechat_login():
    """用微信扫码拿到的 TGC 换取 portal TOKEN（手动粘贴 TGC 模式）。"""
    with _operation_lock:
        castgc = request.json.get("castgc", "").strip()
        if not castgc:
            return jsonify({"ok": False, "message": "未收到 TGC"})
        try:
            secret = load_secret()
            config = load_config()
            token = exchange_castgc_for_token(secret, config, castgc)
            return jsonify({"ok": True, "message": f"登录成功！TOKEN: {token[:16]}..."})
        except Exception as e:
            return jsonify({"ok": False, "message": f"登录失败: {e}"})


@app.route("/api/scan-login/start", methods=["POST"])
def api_scan_login_start():
    """开始扫码登录：连 CAS SSE，返回二维码 URL。"""
    with _scan_lock:
        session_id = uuid.uuid4().hex[:16]
        _scan_sessions[session_id] = {
            "status": "connecting",
            "client_id": None,
            "tgc": None,
            "token": None,
            "token_error": None,
            "error": None,
            "created_at": time.time(),
        }

    secret = load_secret()
    config = load_config()
    th = threading.Thread(
        target=_subscribe_sse,
        args=(session_id, config, secret),
        daemon=True,
    )
    th.start()

    # 等 HELLO 事件（最多等 5 秒）
    for _ in range(25):
        time.sleep(0.2)
        with _scan_lock:
            ses = _scan_sessions.get(session_id)
            if ses and ses.get("client_id"):
                mode = config.get("network_mode", "webvpn")
                if mode == "webvpn":
                    cas_mobie = f"{WEBVPN_BASE}/https/6361732e67647573742e6564752e636e/cas/mobieAuth"
                else:
                    cas_mobie = "https://cas.gdust.edu.cn/cas/mobieAuth"
                qr_url = f"{cas_mobie}?clientId={ses['client_id']}"
                return jsonify({
                    "ok": True,
                    "session_id": session_id,
                    "qr_url": qr_url,
                    "message": "二维码已生成，请用钉钉或微信扫码",
                })
            if ses and ses.get("status") == "error":
                return jsonify({"ok": False, "message": f"SSE 连接失败: {ses.get('error')}"})

    return jsonify({"ok": False, "message": "SSE 连接超时，请重试"})


@app.route("/api/scan-login/check/<session_id>")
def api_scan_login_check(session_id: str):
    """轮询扫码登录状态。"""
    with _scan_lock:
        ses = _scan_sessions.get(session_id)
        if not ses:
            return jsonify({"ok": False, "message": "会话不存在"})

        status = ses.get("status")
        if status == "success" and ses.get("token"):
            token = ses.pop("token")
            return jsonify({"ok": True, "status": "success", "message": f"登录成功！TOKEN: {token[:16]}..."})
        if status == "success" and ses.get("token_error"):
            err = ses.pop("token_error")
            return jsonify({"ok": True, "status": "success", "message": "扫码成功，但换 TOKEN 失败", "error": err})
        if status == "success":
            return jsonify({"ok": True, "status": "success", "message": "扫码成功，正在换 TOKEN...", "waiting_token": True})
        if status == "confirming":
            return jsonify({"ok": True, "status": "confirming", "message": "✅ 正在确认身份..."})
        if status == "need_bind":
            bind_url = f"https://cas.gdust.edu.cn/cas/mobieDDBind?clientId={ses['client_id']}&unionID={ses.get('union_id', '')}"
            return jsonify({"ok": True, "status": "need_bind", "message": "需要绑定钉钉账号", "bind_url": bind_url})
        if status == "error":
            err = ses.get("error", "未知错误")
            return jsonify({"ok": False, "status": "error", "message": f"连接失败: {err}"})
        if status == "waiting_scan":
            return jsonify({"ok": True, "status": "waiting_scan", "message": "等待扫码..."})
        if status == "scanned":
            return jsonify({"ok": True, "status": "scanned", "message": "✅ 已扫码，正在确认..."})
        return jsonify({"ok": True, "status": status, "message": f"当前状态: {status}"})


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    """抓取课表。"""
    with _operation_lock:
        data = request.json
        week = data.get("week")
        all_weeks = data.get("all_weeks", False)

        config = load_config()
        secret = load_secret()
        token = secret.get("portal_token")

        if not token:
            return jsonify({"ok": False, "message": "无 TOKEN，请先登录"})

        s = make_session(secret, config)
        if not token_works(s, token, config, week or current_teaching_week(config)):
            return jsonify({"ok": False, "message": "TOKEN 已失效，请重新登录"})

        if all_weeks:
            weeks = list(range(1, 21))
        elif week:
            weeks = [int(week)]
        else:
            weeks = [current_teaching_week(config)]

        output = DEFAULT_OUTPUT
        results = []
        failures = []

        for w in weeks:
            ok, resp, msg = fetch_week(s, token, config, w)
            if ok and resp:
                courses = resp["data"]["courseList"]
                results.append({"week": w, "count": len(courses), "courses": courses})
            else:
                failures.append({"week": w, "error": msg})
            time.sleep(0.3)

        # 写入文件
        if results:
            week_data = {str(r["week"]): r["courses"] for r in results}
            write_weeks(output, week_data)

        return jsonify({
            "ok": bool(results),
            "results": results,
            "failures": failures,
            "total_courses": sum(r["count"] for r in results),
            "output_file": str(output),
        })


@app.route("/api/timetable")
def api_timetable():
    """读取已抓取的课表数据。"""
    timetable = load_existing_timetable(DEFAULT_OUTPUT)
    if not timetable:
        return jsonify({"ok": False, "message": "暂无课表数据，请先抓取"})
    # 统计
    total = sum(len(v or []) for v in timetable.values())
    weeks = sorted(timetable.keys(), key=lambda x: int(x) if x.isdigit() else 0)
    return jsonify({
        "ok": True,
        "timetable": timetable,
        "total_courses": total,
        "weeks": weeks,
    })


@app.route("/api/download")
def api_download():
    """下载 timetable.json 文件。"""
    if DEFAULT_OUTPUT.exists():
        return send_file(
            DEFAULT_OUTPUT,
            mimetype="application/json",
            as_attachment=True,
            download_name="timetable.json",
        )
    return jsonify({"ok": False, "message": "文件不存在，请先抓取"}), 404


# ──────────────────────────── 启动 ────────────────────────────

if __name__ == "__main__":
    import webbrowser

    print("🎓 GDUST 课表抓取工具 — Web GUI")
    print("🌐 正在打开浏览器...")

    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        webbrowser.open("http://localhost:5000")

    app.run(host="127.0.0.1", port=5000, debug=True)
