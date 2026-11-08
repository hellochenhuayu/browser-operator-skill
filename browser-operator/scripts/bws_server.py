#!/usr/bin/env python3
"""bws_server.py — 浏览器守护进程（仅监听 127.0.0.1，带 token）。

设计要点（安全护栏都在这一层强制执行，模型无法绕过）：
1. 任何非 HTML 文件导航都会被拦截，返回文件元信息，除非带 approved=True（来自用户明确许可）。
2. 页面触发的下载默认一律 cancel，只有 download 命令（要求 user_approved=True）才放行。
3. 密码框 / 账号框不会被 type 命令自动填写，除非 user_provided=True；值永不回显、永不落盘。
4. 每条命令执行后自动截图，返回截图绝对路径。
"""

import argparse
import hashlib
import json
import os
import queue
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RUNTIME = Path(
    os.environ.get("BWS_RUNTIME")
    or (Path.home() / ".workbuddy" / "browser-operator" / "runtime")
)
SHOTS = RUNTIME / "screens"
DOWNLOADS = RUNTIME / "downloads"
PROFILE = RUNTIME / "profile"
CONF = RUNTIME / "server.json"
for _d in (RUNTIME, SHOTS, DOWNLOADS):
    _d.mkdir(parents=True, exist_ok=True)

HTML_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
ENGINES = {
    "bing": ("https://www.bing.com/search?q={q}&setlang=zh-Hans", "#b_results li.b_algo h2 a"),
    "baidu": ("https://www.baidu.com/s?wd={q}", "#content_left h3 a, #content_left .result h3 a"),
    "google": ("https://www.google.com/search?q={q}&hl=zh-CN", "#search a:has(h3), #rso a:has(h3)"),
    "ddg": ("https://duckduckgo.com/html/?q={q}", "#links .result__a, .result__a"),
}
GENERIC_RESULT_SEL = "main h2 a, main h3 a, #content h2 a, #content h3 a, article h2 a"

FIELD_JS = """() => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const out = [];
  for (const el of document.querySelectorAll('input, textarea')) {
    if (!vis(el)) continue;
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden','submit','button','checkbox','radio','file','image','range','search'].includes(type)) continue;
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
    const hint = [el.name, el.id, el.placeholder, el.getAttribute('aria-label'), ac].join(' ');
    const isPwd = type === 'password';
    const isUser = ac === 'username' || /user|email|phone|mobile|account|login|userid|账号|手机|邮箱|用户名/i.test(hint);
    if (!isPwd && !isUser) continue;
    out.push({type: type, name: el.name || '', id: el.id || '',
              placeholder: el.placeholder || '', autocomplete: ac,
              secret: isPwd, label: (el.getAttribute('aria-label') || '')});
  }
  return out;
}"""

LINK_JS = """(lim) => {
  const seen = new Set(); const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href || '';
    if (!/^https?:/i.test(href)) continue;
    const t = (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' ');
    if (!t || seen.has(href)) continue;
    seen.add(href);
    out.push({text: t.slice(0, 160), href: href});
    if (out.length >= lim) break;
  }
  return out;
}"""


FILE_EXT = (".pdf", ".zip", ".rar", ".7z", ".gz", ".tar", ".doc", ".docx", ".xls", ".xlsx",
            ".ppt", ".pptx", ".csv", ".txt", ".exe", ".msi", ".dmg", ".apk", ".ipa",
            ".mp3", ".mp4", ".avi", ".mov", ".wav", ".png", ".jpg", ".jpeg", ".gif",
            ".epub", ".mobi", ".json", ".xml", ".sql", ".iso", ".bin", ".whl")


def _looks_like_file(url: str) -> bool:
    path = url.split("?")[0].split("#")[0].lower()
    return path.endswith(FILE_EXT)


def _safe_name(name: str) -> str:
    name = urllib.parse.unquote(name or "").strip()
    name = os.path.basename(name.split("?")[0].split("#")[0])
    name = "".join(c for c in name if c.isalnum() or c in "._- ()[]（）【】" ).strip()
    return name[:120] or f"download-{int(time.time())}"


def _fmt_size(n) -> str:
    try:
        n = int(n)
    except Exception:
        return "未知"
    return f"{n / 1048576:.2f} MB" if n >= 1048576 else f"{n / 1024:.1f} KB"


class Worker(threading.Thread):
    def __init__(self, channel="msedge", headed=False):
        super().__init__(daemon=True)
        self.channel = channel
        self.headed = headed
        self.q = queue.Queue()
        self.ready = threading.Event()
        self.boot_error = None
        self.page = None
        self.context = None
        self._shot_no = 0
        self._armed = None          # 已授权的一次性下载描述
        self._last_download = None  # 最近一次落盘的下载结果
        self._blocked_download = None
        self._suppress_guard = False

    # ---------- 基础设施 ----------
    def run(self):
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            extra = {}
            if self.channel and self.channel.lower() not in ("none", "chromium", ""):
                extra["channel"] = self.channel   # 复用系统 Chrome/Edge，免下载浏览器
            self.context = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE),
                headless=not self.headed,
                accept_downloads=True,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                args=["--disable-blink-features=AutomationControlled"],
                **extra,
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(30000)
            self.context.on("page", self._attach_guards)
            self._attach_guards(self.page)
            self.ready.set()
        except Exception as e:  # noqa: BLE001
            self.boot_error = f"{type(e).__name__}: {e}"
            self.ready.set()
            return

        while True:
            item = self.q.get()
            if item is None:
                break
            cmd, args, ev, box = item
            try:
                box["r"] = self.dispatch(cmd, args)
            except Exception as e:  # noqa: BLE001
                box["r"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            ev.set()

    def submit(self, cmd, args):
        ev = threading.Event()
        box = {}
        self.q.put((cmd, args, ev, box))
        if not ev.wait(300):
            return {"ok": False, "error": "命令执行超时（300s）"}
        return box.get("r", {"ok": False, "error": "未知错误"})

    def _attach_guards(self, page):
        try:
            page.on("download", self._on_download)
            page.on("dialog", lambda d: d.dismiss())
        except Exception:
            pass

    def _on_download(self, dl):
        """默认取消所有页面触发的下载，除非已获授权。"""
        if self._suppress_guard:
            try:
                dl.cancel()
            except Exception:
                pass
            return
        if not self._armed:
            try:
                dl.cancel()
            except Exception:
                pass
            self._blocked_download = {
                "blocked": True,
                "url": dl.url,
                "suggested_filename": dl.suggested_filename,
                "why": "页面触发了下载，但未获得用户许可，已自动取消",
            }
            return
        try:
            path = DOWNLOADS / _safe_name(dl.suggested_filename or "download")
            if path.exists():
                path = path.with_name(f"{path.stem}-{int(time.time())}{path.suffix}")
            dl.save_as(str(path))
            self._last_download = {
                "blocked": False,
                "url": dl.url,
                "path": str(path),
                "filename": path.name,
                "bytes": path.stat().st_size,
                "reason": self._armed.get("reason"),
            }
        except Exception as e:  # noqa: BLE001
            self._last_download = {"blocked": False, "error": str(e)}
        finally:
            self._armed = None

    def _shot(self, full=False):
        try:
            self._shot_no += 1
            p = SHOTS / f"shot-{time.strftime('%H%M%S')}-{self._shot_no:03d}.png"
            self.page.screenshot(path=str(p), full_page=full)
            return str(p)
        except Exception as e:  # noqa: BLE001
            return f"(截图失败: {e})"

    def _fields(self):
        try:
            return self.page.evaluate(FIELD_JS) or []
        except Exception:
            return []

    def _state(self, shot_full=False, text_max=1500):
        st = {"ok": True, "url": None, "title": None, "screenshot": None}
        try:
            st["url"] = self.page.url
            st["title"] = self.page.title()
        except Exception:
            pass
        fields = self._fields()
        st["credential_fields"] = fields
        st["auth_required"] = any(f.get("secret") or f.get("autocomplete") == "username" for f in fields)
        if st["auth_required"]:
            st["auth_hint"] = "检测到账号/密码输入框：必须先停下来询问用户，禁止自行猜测或编造。"
        if text_max:
            try:
                body = self.page.inner_text("body")
                body = "\n".join(l.strip() for l in body.splitlines() if l.strip())
                st["text"] = body[:text_max] + ("…" if len(body) > text_max else "")
            except Exception:
                st["text"] = ""
        if st.get("url") in ("about:blank", "", None):
            st["hint"] = ("当前没有打开的页面。若刚才访问过的页面消失了，说明浏览器会话被重启过——"
                          "请用后台任务方式重启 bws_server.py，页面状态才能跨步骤保留。")
        st["screenshot"] = self._shot(full=shot_full)
        if self._blocked_download:
            st["blocked_download"] = self._blocked_download
            self._blocked_download = None
        return st

    # ---------- 命令分发 ----------
    def dispatch(self, cmd, a):
        fn = getattr(self, "cmd_" + cmd, None)
        if not fn:
            return {"ok": False, "error": f"未知命令: {cmd}"}
        return fn(a or {})

    def cmd_status(self, a):
        return self._state(text_max=200)

    def cmd_open(self, a):
        url = a.get("url", "").strip()
        if not url:
            return {"ok": False, "error": "缺少 url"}
        if "://" not in url:
            url = "https://" + url
        if url.startswith("file:") and not a.get("approved"):
            pass  # 本地 HTML 直接放行
        meta = self._probe(url)
        ctype = (meta.get("content_type") or "").lower()
        is_html = (not ctype) or any(t in ctype for t in HTML_TYPES)
        if not is_html:
            if not a.get("approved"):
                st = self._state(text_max=0)
                st.update({
                    "ok": False,
                    "blocked": "file_navigation",
                    "file": meta,
                    "size_human": _fmt_size(meta.get("content_length")),
                    "message": "该链接不是网页而是文件。必须先向用户报告文件信息与下载理由，取得许可后再用 download 命令。",
                })
                return st
        self.page.goto(url, wait_until="domcontentloaded")
        self._settle(min_text=200)
        st = self._state(text_max=int(a.get("text_max", 2000)))
        st["page_type"] = "html" if is_html else "file"
        return st

    def cmd_search(self, a):
        q = a.get("query", "").strip()
        if not q:
            return {"ok": False, "error": "缺少 query"}
        name = a.get("engine", "bing")
        tpl, sel = ENGINES.get(name, ENGINES["bing"])
        self.page.goto(tpl.format(q=urllib.parse.quote(q)), wait_until="domcontentloaded")
        self._settle(selector=sel, min_text=400)
        results = self._extract_results(sel, int(a.get("limit", 10)))
        st = self._state(text_max=600)
        st["engine"] = name
        st["query"] = q
        st["results"] = results
        if not results:
            st["warning"] = "未解析到结果，可用 text / links 命令查看页面实际内容（可能有验证码或改版）。"
        return st

    def cmd_snapshot(self, a):
        return self._state(shot_full=bool(a.get("full")), text_max=int(a.get("text_max", 3000)))

    def cmd_text(self, a):
        st = self._state(text_max=int(a.get("max", 6000)))
        return st

    def cmd_links(self, a):
        lim = int(a.get("limit", 60))
        links = self.page.evaluate(LINK_JS, lim) or []
        f = (a.get("filter") or "").lower()
        if f:
            links = [l for l in links if f in l["text"].lower() or f in l["href"].lower()]
        st = self._state(text_max=0)
        st["links"] = links
        return st

    def cmd_click(self, a):
        target = a.get("target", "")
        if not target:
            return {"ok": False, "error": "缺少 target（CSS 选择器或 text=文本）"}
        prev = self.page.url
        try:
            self._locator(target).first.click(timeout=20000)
        except Exception as e:  # noqa: BLE001
            st = self._state(text_max=600)
            st.update({"ok": False, "error": f"点击失败: {e}",
                       "hint": "target 支持 CSS 选择器或 text=文本。下面是当前页可点击链接，可改用 CSS 选择器或更精确的文字。"})
            try:
                st["links"] = self.page.evaluate(LINK_JS, 15)
            except Exception:
                pass
            return st
        self._after_action()
        self._restore_if_blank(prev)
        if self.page.url != prev and _looks_like_file(self.page.url):
            meta = self._probe(self.page.url)
            try:
                self.page.go_back(wait_until="domcontentloaded")
                self._settle(ms=400)
            except Exception:
                pass
            st = self._state(text_max=0)
            st.update({"ok": False, "blocked": "file_navigation", "file": meta,
                       "size_human": _fmt_size(meta.get("content_length")),
                       "message": "这个链接指向文件而不是网页，已拦截并退回原页面。先向用户报告文件名称/类型/大小/来源/用途，取得同意后用 download 命令。"})
            return st
        st = self._state(text_max=1200)
        if self._last_download:
            st["download"] = self._last_download
            self._last_download = None
        return st

    def cmd_type(self, a):
        sel, val = a.get("selector", ""), a.get("text", "")
        loc = self._locator(sel).first
        risky = self._is_credential_target(sel)
        if risky and not a.get("user_provided"):
            st = self._state(text_max=0)
            st.update({
                "ok": False,
                "blocked": "credential_field",
                "message": "目标是账号/密码输入框。必须先向用户索取，用户提供后用 fill-login 命令（user_provided=true）填写。",
            })
            return st
        loc.fill(val, timeout=20000)
        if a.get("enter"):
            loc.press("Enter")
            self._after_action()
        st = self._state(text_max=800)
        if risky:
            st["note"] = "已填写敏感字段，内容不回显。"
        return st

    def cmd_fill_login(self, a):
        """凭据经客户端 stdin 读入、走本机回环传入，值不写入响应、不落盘、不进日志。"""
        data = a
        if not data.get("user_provided"):
            return {"ok": False, "blocked": "credential_field",
                    "message": "需用户本人提供凭据后才能填写（user_provided=true）。"}
        filled = []
        for key, field in (("username_selector", "username"), ("password_selector", "password")):
            s = data.get(key) or ""
            v = data.get(field)
            if s and v:
                try:
                    self._locator(s).first.fill(str(v), timeout=15000)
                    filled.append(field)
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "error": f"{field} 填写失败: {e}", "filled": filled}
        if data.get("submit"):
            btn = data.get("submit_selector")
            try:
                if btn:
                    self._locator(btn).first.click(timeout=15000)
                else:
                    self.page.keyboard.press("Enter")
                self.page.wait_for_timeout(1500)
                self._settle()
            except Exception:
                pass
        st = self._state(text_max=1200)
        st["filled"] = filled
        st["note"] = "凭据内容已填写且未回显/未落盘。"
        return st

    def cmd_press(self, a):
        self.page.keyboard.press(a.get("key", "Enter"))
        self._after_action()
        return self._state(text_max=800)

    def cmd_scroll(self, a):
        self.page.mouse.wheel(0, int(a.get("dy", 800)))
        self.page.wait_for_timeout(600)
        return self._state(text_max=800)

    def cmd_back(self, a):
        self.page.go_back(wait_until="domcontentloaded")
        self._settle()
        return self._state(text_max=1200)

    def cmd_forward(self, a):
        self.page.go_forward(wait_until="domcontentloaded")
        self._settle()
        return self._state(text_max=1200)

    def cmd_reload(self, a):
        self.page.reload(wait_until="domcontentloaded")
        self._settle()
        return self._state(text_max=1200)

    def cmd_tabs(self, a):
        pages = self.context.pages
        idx = a.get("switch")
        if idx is not None:
            i = int(idx)
            if 0 <= i < len(pages):
                self.page = pages[i]
                self._attach_guards(self.page)
                self.page.bring_to_front()
                return self._state(text_max=1200)
            return {"ok": False, "error": f"标签页索引越界，共 {len(pages)} 个"}
        st = self._state(text_max=0)
        st["tabs"] = [{"index": i, "url": p.url, "title": (p.title() or "")[:80],
                       "current": p is self.page} for i, p in enumerate(pages)]
        return st

    def cmd_close_tab(self, a):
        if len(self.context.pages) <= 1:
            return {"ok": False, "error": "只剩一个标签页，无需关闭"}
        old = self.page
        old.close()
        self.page = self.context.pages[-1]
        self._attach_guards(self.page)
        return self._state(text_max=800)

    def cmd_screenshot(self, a):
        p = self._shot(full=bool(a.get("full")))
        return {"ok": True, "screenshot": p, "url": self.page.url, "title": self.page.title()}

    def cmd_probe(self, a):
        url = a.get("url", "")
        meta = self._probe(url)
        st = self._state(text_max=0)
        st["file"] = meta
        st["is_file"] = not any(t in (meta.get("content_type") or "").lower() for t in HTML_TYPES)
        st["size_human"] = _fmt_size(meta.get("content_length"))
        return st

    def cmd_download(self, a):
        """唯一放行下载的入口，必须带 reason + user_approved。"""
        reason = (a.get("reason") or "").strip()
        max_mb = float(a.get("max_mb", 500))
        if not a.get("user_approved"):
            st = self._state(text_max=0)
            st.update({
                "ok": False,
                "blocked": "no_user_consent",
                "message": "下载未经用户许可。先把文件名称/类型/大小/来源/用途报告给用户，得到明确同意后再带上 user_approved=true 重试。",
            })
            return st
        if not reason:
            st = self._state(text_max=0)
            st.update({"ok": False, "blocked": "no_reason",
                       "message": "必须填写下载理由 reason（向用户说明过的用途）。"})
            return st

        url, sel = a.get("url"), a.get("click")
        prev = self.page.url

        if sel:
            self._armed = {"reason": reason}
            self._last_download = None
            clicked = True
            try:
                self._locator(sel).first.click(timeout=20000)
                for _ in range(120):
                    if self._last_download or self._blocked_download:
                        break
                    self.page.wait_for_timeout(500)
            except Exception:
                clicked = False
            finally:
                self._armed = None
            if clicked and self._last_download:
                r = self._last_download
                self._last_download = None
                r.update(ok=True, size_human=_fmt_size(r.get("bytes")))
                self._restore_if_blank(prev)
                st = self._state(text_max=400)
                st["download"] = r
                st["how"] = "click-download-event"
                return st
            page_url = self.page.url
            self._blocked_download = None
            if not clicked:
                st = self._state(text_max=600)
                st.update({"ok": False, "error": "点击目标失败，未触发下载",
                           "hint": "改用 CSS 选择器，或先用 links 命令拿到文件直链后用 --url 下载。"})
                return st
            # 有些浏览器/站点点击后是「跳转到文件」而不是下载事件，回退为直链下载
            if _looks_like_file(page_url):
                try:
                    if prev and prev != "about:blank":
                        self.page.goto(prev, wait_until="domcontentloaded")
                        self._settle(ms=300)
                except Exception:
                    pass
                return self._fetch_download(page_url, reason, max_mb)
            st = self._state(text_max=400)
            st["download"] = {"ok": False, "error": "点击后没有捕获到下载事件，页面也没有跳转到文件"}
            return st

        if not url:
            return {"ok": False, "error": "缺少 url 或 click"}
        return self._fetch_download(url, reason, max_mb)

    def _fetch_download(self, url, reason, max_mb):
        meta = self._probe(url)
        size = meta.get("content_length") or 0
        if size and size > max_mb * 1048576:
            st = self._state(text_max=0)
            st.update({"ok": False, "blocked": "too_large", "file": meta,
                       "message": f"文件 {_fmt_size(size)} 超过 {max_mb}MB 上限，请与用户确认是否调高 max_mb。"})
            return st
        resp = self.context.request.get(url, timeout=180000)
        if not resp.ok:
            st = self._state(text_max=0)
            st.update({"ok": False, "error": f"HTTP {resp.status}", "file": meta})
            return st
        body = resp.body()
        cd = resp.headers.get("content-disposition", "") or ""
        fname = ""
        for part in cd.split(";"):
            if "filename=" in part:
                fname = part.split("filename=")[-1].strip().strip('"\'')
        fname = _safe_name(fname or meta.get("filename") or url)
        path = DOWNLOADS / fname
        if path.exists():
            path = path.with_name(f"{path.stem}-{int(time.time())}{path.suffix}")
        path.write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        if self.page.url in ("about:blank", ""):
            self._suppress_guard = True
            try:
                self.page.goto(url, wait_until="domcontentloaded")
                self._settle(ms=300)
            except Exception:
                pass
            finally:
                self._suppress_guard = False
                self._blocked_download = None
        st = self._state(text_max=0)
        st["download"] = {
            "ok": True,
            "path": str(path),
            "filename": path.name,
            "bytes": len(body),
            "size_human": _fmt_size(len(body)),
            "content_type": resp.headers.get("content-type", ""),
            "final_url": resp.url,
            "sha256": digest,
            "reason": reason,
        }
        return st

    def cmd_shutdown(self, a):
        def _die():
            time.sleep(0.3)
            os._exit(0)
        threading.Thread(target=_die, daemon=True).start()
        return {"ok": True, "message": "浏览器已关闭"}

    # ---------- 辅助 ----------
    def _restore_if_blank(self, prev):
        """某些下载链接会把页面顶成 about:blank，这里复原到原页面。"""
        try:
            if prev and prev != "about:blank" and self.page.url in ("about:blank", ""):
                self.page.goto(prev, wait_until="domcontentloaded")
                self._settle(ms=300)
        except Exception:
            pass

    def _after_action(self, ms=600):
        """动作后若弹出新标签页（target=_blank），自动切换过去并补挂护栏。"""
        self._settle(ms=ms)
        try:
            pages = self.context.pages
            if len(pages) > 1 and pages[-1] is not self.page:
                self.page = pages[-1]
                self._attach_guards(self.page)
                self._settle(ms=ms)
        except Exception:
            pass

    def _settle(self, ms=600, selector=None, min_text=0):
        """等待页面真正渲染完成：DOM → 目标选择器 → 正文长度稳定。"""
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        if selector:
            try:
                self.page.wait_for_selector(selector, timeout=15000)
            except Exception:
                pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        try:
            for _ in range(20):
                n = self.page.evaluate("() => (document.body && document.body.innerText || '').length")
                if n >= max(min_text, 200):
                    break
                self.page.wait_for_timeout(400)
        except Exception:
            pass
        self.page.wait_for_timeout(ms)

    def _locator(self, target):
        if target.startswith("text="):
            t = target[5:].strip()
            try:
                link = self.page.get_by_role("link", name=t)
                if link.count() > 0:
                    return link
            except Exception:
                pass
            return self.page.get_by_text(t, exact=False)
        if target.startswith("role="):
            return self.page.get_by_role(target[5:].strip())
        return self.page.locator(target)

    def _is_credential_target(self, sel):
        try:
            t = self.page.locator(sel).first
            typ = (t.get_attribute("type") or "").lower()
            if typ == "password":
                return True
            blob = " ".join(filter(None, [t.get_attribute("name"), t.get_attribute("id"),
                                          t.get_attribute("autocomplete"), t.get_attribute("placeholder")])).lower()
            return bool(typ in ("email", "tel")) or "username" in blob or "user" in blob or "账号" in blob
        except Exception:
            return False

    def _probe(self, url):
        meta = {"url": url, "final_url": url, "content_type": "", "content_length": 0,
                "filename": os.path.basename(urllib.parse.urlparse(url).path) or ""}
        try:
            r = self.context.request.get(url, timeout=45000, max_redirects=5)
            h = r.headers
            meta["final_url"] = r.url
            meta["status"] = r.status
            meta["content_type"] = (h.get("content-type") or "").split(";")[0].strip()
            cl = h.get("content-length")
            meta["content_length"] = int(cl) if cl and cl.isdigit() else len(r.body()) if r.ok else 0
            cd = h.get("content-disposition") or ""
            if "filename=" in cd:
                meta["filename"] = cd.split("filename=")[-1].strip().strip('"\'')
            meta["filename"] = _safe_name(meta["filename"] or "index.html")
        except Exception as e:  # noqa: BLE001
            meta["error"] = str(e)
        return meta

    def _extract_results(self, sel, limit):
        js = """(sel) => {
          const out = []; const seen = new Set();
          let nodes = [];
          try { nodes = [...document.querySelectorAll(sel)]; } catch (e) { nodes = []; }
          if (!nodes.length) nodes = [...document.querySelectorAll('%s')];
          for (const a of nodes) {
            const href = a.href || ''; if (!/^https?:/i.test(href) || seen.has(href)) continue;
            seen.add(href);
            const box = a.closest('li, div.result, article, .b_algo, .result') || a.parentElement;
            out.push({title: ((a.innerText || a.textContent || '')).trim().slice(0,180), url: href,
                      snippet: ((box && (box.innerText || box.textContent)) || '').trim().replace(/\\s+/g,' ').slice(0,260)});
            if (out.length >= 20) break;
          }
          return out;
        }""" % GENERIC_RESULT_SEL
        try:
            items = self.page.evaluate(js, sel) or []
        except Exception:
            items = []
        return items[:limit]


def handler_factory(worker, token):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, obj, code=200):
            data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                return self._send({"ok": True, "pid": os.getpid()})
            return self._send({"ok": False, "error": "use POST /cmd"}, 404)

        def do_POST(self):
            if self.path != "/cmd":
                return self._send({"ok": False, "error": "not found"}, 404)
            if self.headers.get("X-BWS-Token") != token:
                return self._send({"ok": False, "error": "invalid token"}, 403)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:  # noqa: BLE001
                return self._send({"ok": False, "error": f"bad json: {e}"}, 400)
            if worker.boot_error:
                return self._send({"ok": False, "error": f"浏览器启动失败: {worker.boot_error}"})
            self._send(worker.submit(payload.get("cmd"), payload.get("args") or {}))

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--channel", default=os.environ.get("BWS_CHANNEL", "msedge"))
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()

    worker = Worker(channel=a.channel, headed=a.headed)
    worker.start()
    worker.ready.wait(90)
    if worker.boot_error:
        print(json.dumps({"ok": False, "error": worker.boot_error}, ensure_ascii=False))
        sys.exit(1)

    token = secrets.token_hex(16)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), handler_factory(worker, token))
    srv.daemon_threads = True
    CONF.write_text(json.dumps({"port": srv.server_address[1], "token": token,
                                "pid": os.getpid(), "channel": a.channel,
                                "headed": a.headed}, ensure_ascii=False), encoding="utf-8")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
