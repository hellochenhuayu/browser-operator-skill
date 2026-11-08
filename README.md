# browser-operator

> 让 AI 助手真正会用浏览器：搜索、阅读网页、下载文件，并且**每一步都得先问你**。

一个 WorkBuddy / Claude Skill：用本机真实浏览器（Edge / Chrome）+ Playwright 完成「搜索 → 打开 → 阅读 → 下载」的闭环。

它不是"给个 URL 就去抓"的爬虫。它的设计前提是：**AI 操作你的浏览器时，必须把控制权留在你手里。**

## 为什么需要它

让 AI 上网，最怕三件事：悄悄下了一堆文件、自己猜账号密码往里填、以及"做完了"但你根本不知道它做了什么。

browser-operator 把这三件事变成代码层面的硬约束 —— 不是写在提示词里"请记得询问用户"，而是脚本直接返回 `blocked`，模型想绕也绕不过去。

## 三条硬约束

| 约束 | 实现方式 | 触发时的返回 |
| --- | --- | --- |
| **下载必须先获许可** | 所有页面下载事件默认被 `cancel()`；`download` 命令强制要求 `--user-approved` + `--reason`；非 HTML 链接导航即拦截 | `blocked: no_user_consent` / `file_navigation` |
| **凭据只能由用户提供** | 自动识别 `input[type=password]` 与用户名框；`type` 命中即拒绝；只能走 `fill-login` 从 stdin 传入，不回显、不落盘 | `blocked: credential_field` |
| **每步都要出图** | 每条命令执行后自动截图，返回绝对路径 | `screenshot` 字段 |

## 特性

- **搜索**：Bing / Baidu / Google / DuckDuckGo，结构化返回标题、链接、摘要
- **阅读**：正文提取、链接抓取、懒加载自动滚动补偿
- **交互**：点击（含 `target=_blank` 自动跟随）、输入、按键、滚动、标签页管理
- **下载**：先 `probe` 拿文件名/类型/大小，再经你同意后落盘，返回 SHA-256
- **下载审批**：任何文件在落地前都要过一道人工确认
- **凭据保护**：密码永不进入命令行参数、日志或磁盘
- **会话常驻**：守护进程保持页面状态，支持多步骤流程（登录 → 翻页 → 下载）

## 安装

**方式一：解压到技能目录（最简）**

```bash
# 下载 browser-operator.zip 后
unzip browser-operator.zip -d ~/.workbuddy/skills/
# 重启会话，让技能清单刷新
```

**方式二：克隆并软链接**

```bash
git clone https://github.com/<you>/browser-operator-skill.git
ln -s "$PWD/browser-operator-skill/browser-operator" ~/.workbuddy/skills/browser-operator
```

**依赖**

```bash
python -m pip install playwright        # 唯一依赖
# Windows / macOS 上有 Edge/Chrome 即可，无需 playwright install
# 纯 Linux 无浏览器时：
python -m playwright install chromium
```

## 快速开始

浏览器必须**常驻**，页面状态才能跨步骤保留 —— 用后台方式启动守护进程：

```bash
python ~/.workbuddy/skills/browser-operator/scripts/bws_server.py --port 0
```

> 在 WorkBuddy 里，用 Bash 工具的「后台运行」执行这一行；不要用 `bws.py start`（子进程会被回收，导致每步都从空白页开始）。

验证并就绪：

```bash
python ~/.workbuddy/skills/browser-operator/scripts/bws.py status
```

然后就可以正常使用了 —— 直接对助手说"帮我搜一下 X"、"把这个页面上的 PDF 下载下来"。

## 命令一览

| 命令 | 说明 |
| --- | --- |
| `status` / `start` / `restart` / `stop` | 会话状态与生命周期 |
| `search "<关键词>" [--engine bing\|baidu\|google\|ddg] [--limit 10]` | 搜索 |
| `open <url> [--approved]` | 打开网页（文件链接默认拦截） |
| `snapshot [--full]` / `text [--max N]` | 截图 + 正文 / 仅正文 |
| `links [--filter pdf] [--limit 60]` | 抓取页面链接，找下载直链 |
| `click "<CSS或text=...>"` / `type` / `press` / `scroll` | 交互 |
| `tabs [--switch N]` / `close-tab` | 标签页 |
| `probe <url>` | 只看文件元信息，不下载（**下载前必用**） |
| `download --url <url> --reason "<用途>" --user-approved` | 直链下载 |
| `download --click "<选择器>" --reason "<用途>" --user-approved` | 点击按钮触发下载 |
| `fill-login`（stdin JSON） | 用户提供凭据后填写 |

## 一次典型下载长什么样

```
probe <url>                    → {filename: "报告.pdf", size_human: "2.4 MB", content_type: "application/pdf"}
[向用户报告：文件名/类型/大小/来源/用途，等一句"可以"]
download --url ... --reason "用户要做竞品分析" --user-approved
                               → {path: "...", bytes: 2516582, sha256: "..."}
[把下载后的页面截图发给用户]
```

用户说"不用了"，就停在这里 —— 只给页面截图和要点，不落盘。

## 架构

```
bws.py  ──HTTP(127.0.0.1 + token)──▶  bws_server.py  ──Playwright──▶  Edge/Chrome
(CLI)                                 (常驻守护进程)
```

- 守护进程只监听回环地址，带随机 token；自动绕过系统代理，避免 `127.0.0.1` 被 `HTTP_PROXY` 拦成 502
- 运行时目录 `~/.workbuddy/browser-operator/runtime/`：`screens/` 截图、`downloads/` 下载、`profile/` 浏览器资料

## 已知限制

- 无头模式会被部分站点识别（重滑块验证）；`--headed` 可切可见窗口
- JS 骨架屏站点，截图可能只有头部；用 `scroll` 触发懒加载后再 `text`
- 强反爬站点（Cloudflare 挑战等）成功率有限，属浏览器自动化通病
- 依赖系统已安装的浏览器；纯 Linux 需先 `playwright install chromium`

## License

MIT © 2026 browser-operator contributors
