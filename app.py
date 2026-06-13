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
from pathlib import Path
from threading import Lock

from flask import Flask, jsonify, render_template, request, send_file

# 将当前目录加入 path，确保能 import 主脚本
sys.path.insert(0, str(Path(__file__).parent))
from gdust_timetable import (
    CAPTCHA_PATH,
    DEFAULT_OUTPUT,
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
