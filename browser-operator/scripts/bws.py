#!/usr/bin/env python3
"""bws.py — browser-operator 命令行客户端（自动拉起本地浏览器守护进程）。

示例：
    python bws.py search "2026 新能源汽车销量" --engine bing
    python bws.py snapshot
    python bws.py probe https://example.com/report.pdf
    python bws.py download --url https://example.com/report.pdf --reason "用户要用它做竞品分析" --user-approved
    echo '{"username":"a","password":"b","...":"..."}' | python bws.py fill-login
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 本机回环必须绕过系统代理（否则会 502）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

RUNTIME = Path(os.environ.get("BWS_RUNTIME") or (Path.home() / ".workbuddy" / "browser-operator" / "runtime"))
CONF = RUNTIME / "server.json"
LOG = RUNTIME / "server.log"
SERVER_PY = Path(__file__).with_name("bws_server.py")


def _conf():
    try:
        return json.loads(CONF.read_text(encoding="utf-8"))
    except Exception:
        return None


def _health(conf, timeout=1.5):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{conf['port']}/health")
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _kill_stale(conf):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(conf["pid"]), "/F"],
                           capture_output=True, timeout=10)
        else:
            os.kill(int(conf["pid"]), 9)
    except Exception:
        pass


def start(headed=False, channel=None, force=False):
    conf = _conf()
    if conf and _health(conf) and not force:
        return conf
    if conf and force:
        _kill_stale(conf)
        try:
            CONF.unlink()
        except Exception:
            pass
    RUNTIME.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(SERVER_PY), "--port", "0"]
    if headed:
        cmd.append("--headed")
    if channel:
        cmd += ["--channel", channel]
    env = dict(os.environ, BWS_RUNTIME=str(RUNTIME), PYTHONIOENCODING="utf-8",
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    with open(LOG, "ab") as log:
        subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=env, **kw)
    deadline = time.time() + 90
    while time.time() < deadline:
        time.sleep(0.5)
        c = _conf()
        if c and _health(c):
            return c
        if LOG.exists() and LOG.stat().st_size:
            tail = LOG.read_text(encoding="utf-8", errors="ignore")[-600:]
            if "Error" in tail or "Traceback" in tail:
                raise RuntimeError(f"守护进程启动失败：\n{tail}")
    raise RuntimeError(f"启动超时，日志：{LOG}")


def call(cmd, args=None, timeout=320):
    conf = start()
    payload = json.dumps({"cmd": cmd, "args": args or {}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{conf['port']}/cmd",
        data=payload,
        headers={"Content-Type": "application/json", "X-BWS-Token": conf["token"]},
    )
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"连接守护进程失败: {e}. 可尝试 python bws.py restart"}


def main():
    ap = argparse.ArgumentParser(description="browser-operator CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start"); p.add_argument("--headed", action="store_true"); p.add_argument("--channel")
    p = sub.add_parser("restart"); p.add_argument("--headed", action="store_true"); p.add_argument("--channel")
    sub.add_parser("stop")
    sub.add_parser("status")
    sub.add_parser("snapshot"); 
    sub.choices["snapshot"].add_argument("--full", action="store_true"); sub.choices["snapshot"].add_argument("--text-max", type=int, default=3000)
    sub.add_parser("text"); sub.choices["text"].add_argument("--max", type=int, default=6000)
    p = sub.add_parser("screenshot"); p.add_argument("--full", action="store_true")

    p = sub.add_parser("open"); p.add_argument("url"); p.add_argument("--approved", action="store_true"); p.add_argument("--text-max", type=int, default=2000)
    p = sub.add_parser("search"); p.add_argument("query"); p.add_argument("--engine", default="bing", choices=["bing", "baidu", "google", "ddg"]); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("links"); p.add_argument("--filter"); p.add_argument("--limit", type=int, default=60)
    p = sub.add_parser("click"); p.add_argument("target")
    p = sub.add_parser("type"); p.add_argument("selector"); p.add_argument("text"); p.add_argument("--enter", action="store_true"); p.add_argument("--user-provided", action="store_true")
    p = sub.add_parser("press"); p.add_argument("key", nargs="?", default="Enter")
    p = sub.add_parser("scroll"); p.add_argument("--dy", type=int, default=800)
    sub.add_parser("back"); sub.add_parser("forward"); sub.add_parser("reload")
    p = sub.add_parser("tabs"); p.add_argument("--switch", type=int)
    sub.add_parser("close-tab")

    p = sub.add_parser("probe"); p.add_argument("url")
    p = sub.add_parser("download")
    p.add_argument("--url"); p.add_argument("--click"); p.add_argument("--reason", required=True)
    p.add_argument("--user-approved", action="store_true"); p.add_argument("--max-mb", type=float, default=500)

    p = sub.add_parser("fill-login")
    p.add_argument("--stdin", action="store_true", help="从 stdin 读取 JSON（默认行为）")

    a = ap.parse_args()
    c = a.cmd

    if c == "start":
        print(json.dumps(start(a.headed, a.channel), ensure_ascii=False)); return
    if c == "restart":
        print(json.dumps(start(a.headed, a.channel, force=True), ensure_ascii=False)); return
    if c == "stop":
        conf = _conf()
        if conf and _health(conf):
            call("shutdown")
            try:
                CONF.unlink()
            except Exception:
                pass
            print(json.dumps({"ok": True, "message": "已关闭"}, ensure_ascii=False))
        else:
            print(json.dumps({"ok": True, "message": "没有运行中的会话"}, ensure_ascii=False))
        return

    if c == "fill-login":
        raw = sys.stdin.read()
        try:
            data = json.loads(raw) if raw.strip() else {}
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"stdin 必须是合法 JSON: {e}"}, ensure_ascii=False)); return
        data["user_provided"] = True
        print(json.dumps(call("fill_login", data), ensure_ascii=False, indent=2)); return

    payload = {k: v for k, v in vars(a).items() if k not in ("cmd",)}
    mapping = {"text-max": "text_max", "user-provided": "user_provided",
               "max-mb": "max_mb", "user_approved": "user_approved"}
    keys = {"text_max": "text_max", "user_provided": "user_provided", "max_mb": "max_mb",
            "approved": "approved", "full": "full", "enter": "enter",
            "engine": "engine", "limit": "limit", "filter": "filter", "dy": "dy",
            "url": "url", "click": "click", "reason": "reason",
            "user_approved": "user_approved", "target": "target",
            "selector": "selector", "text": "text", "max": "max", "key": "key"}
    args = {}
    for k, v in payload.items():
        if v is None:
            continue
        args[keys.get(k, k)] = v
    if c == "search":
        args["query"] = args.pop("query", None) or a.query
    if c == "open":
        args["url"] = a.url

    print(json.dumps(call(c.replace("-", "_"), args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
