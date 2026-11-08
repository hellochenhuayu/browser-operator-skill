---
name: browser-operator
description: 用本机真实浏览器（Edge/Chrome + Playwright）完成网页搜索、页面阅读与文件下载。当用户要求"上网搜一下/查资料/打开某网站看看/登录某网站后操作/把网页上的文件下载下来/截图看页面"时使用。内置三条强制规则：下载前必须先向用户报告文件名称、类型、大小、来源与下载理由并取得明确许可；遇到账号密码框必须停下让用户本人提供；每次操作后都必须把页面截图发给用户。
---

# 浏览器操作（browser-operator）

用真实浏览器替用户完成「搜索 → 打开 → 阅读 → 下载」的闭环，所有危险动作都由脚本层强制拦截，无法靠"忘了问"绕过。

## 一、准备（每次会话首次使用执行一次）

**关键：浏览器必须常驻，页面状态才能跨步骤保留。** 用 Bash 工具以「后台运行」方式启动守护进程：

```
command:          python ~/.workbuddy/skills/browser-operator/scripts/bws_server.py --port 0
run_in_background: true
```

随后验证（返回 `ok: true` 且带截图即就绪）：

```bash
python ~/.workbuddy/skills/browser-operator/scripts/bws.py status
```

- 之后的每条命令都用 `bws.py`，它会自动连上这个常驻进程。
- 不要用 `bws.py start` 作为主路径：它用子进程方式拉起，在部分沙箱环境里进程会随命令结束被回收，导致每一步都从空白页开始（表现为"刚打开的页面没了"）。
- 依赖：`python -m pip install playwright`（已装可跳过）；默认复用系统 Edge/Chrome，无需 `playwright install`。
- 非 Windows / 无 Edge 的机器：先 `python -m playwright install chromium`，启动守护进程时加 `--channel none`（用内置 Chromium）；有 Chrome 则可用 `--channel chrome`。
- 只跑一两步时可以偷懒用 `bws.py start`（同一条命令内链式执行有效）。
- 结束用 `bws.py stop`；异常用 `bws.py restart`。
- 运行目录：`~/.workbuddy/browser-operator/runtime/`（`screens/` 截图、`downloads/` 下载文件、`server.log` 日志）。

## 二、三条铁律（不可协商）

### 1. 下载必须先报告 + 取得许可
任何文件（pdf/zip/docx/xlsx/csv/exe/图片/音视频…）在落盘前，必须做三件事，缺一不可：

1. **先探测**：`bws.py probe <url>`，拿到 `content_type`、`content_length`/`size_human`、`filename`、`final_url`。
2. **先报告**：把下面 5 项告诉用户，并说明**为什么下载**：
   - 文件名称、文件类型、文件大小
   - 来源网址 / 来自哪个页面（用户是在哪一步看到的）
   - 下载用途（用它能解决用户的什么问题，例如"用户要做竞品分析，需要这份 2026 年销量报告的数据")
   - 保存位置
3. **等明确同意**：只有用户明确说"下/可以/下载吧"之后，才执行
   `bws.py download --url <url> --reason "<向用户说明过的用途>" --user-approved`

用户说"不用了/先别下/我只想看看"，就**不下载**，改为把页面截图 + 正文要点给到用户。

脚本层面的兜底（会直接拒绝，属正常现象，不要尝试绕过）：
- `blocked: no_user_consent` → 缺 `--user-approved`
- `blocked: no_reason` → 缺 `--reason`
- `blocked: file_navigation` → 该链接是文件不是网页，已拦截（点链接也会被拦并退回原页面）
- `blocked: too_large` → 超过 500MB 上限，需与用户确认后用 `--max-mb` 调高

### 2. 账号密码：只由用户提供
- 任何命令返回 `auth_required: true` 或 `credential_fields` 非空时，**立即停下**，把截图发给用户并直接问：
  > 这一页需要登录。请把用户名和密码发给我，我填完就继续（我只用于这次填写，不会保存）。
- 用户不给就停在这里，不猜、不试默认密码、不注册账号、不"绕过登录"。
- 用户给了之后，只用 `fill-login` 从 **stdin** 传入，禁止写进命令行参数、文件或脚本：

```bash
echo '{"username_selector":"#login_field","password_selector":"#password","username":"<用户给的>","password":"<用户给的>","submit":true}' \
  | python ~/.workbuddy/skills/browser-operator/scripts/bws.py fill-login
```

- `bws.py type` 命中密码/账号框会直接 `blocked: credential_field`，这是设计而非故障。
- 填写后的截图要发给用户，并说明"已填写，未回显、未保存"。

### 3. 每一步都要发截图
每条命令的返回 JSON 都带 `screenshot`（绝对路径）。**每次操作后都要**：

1. 用 `ViewImage` 看一下截图，确认页面确实到了预期位置（顺便发现验证码/报错）。
2. 用 `present_files` 把这张 PNG 发给用户，配 1–3 句说明：现在在哪个页面、看到了什么、下一步做什么。

不要只贴 JSON、不要只说"已完成"而不给图。唯一例外：冷启动后第一次 `status` 若还是空白页（尚未打开任何网站），可只做文字说明。

## 三、命令速查

| 命令 | 说明 |
| --- | --- |
| `start` / `restart` / `stop` / `status` | 启停浏览器、查看当前状态 |
| `search "<关键词>" [--engine bing\|baidu\|google\|ddg] [--limit 10]` | 搜索并结构化返回 `results`（title/url/snippet） |
| `open <url> [--approved]` | 打开网页；`--approved` 仅用于用户已同意直接打开文件类链接 |
| `snapshot [--full] [--text-max 3000]` | 截图 + 正文 + 表单检测 |
| `text [--max 6000]` | 只要正文（长文阅读时用，比 snapshot 省 token） |
| `links [--filter 关键词] [--limit 60]` | 抓取页面全部链接（找下载直链、找分页/详情页） |
| `click "<CSS 选择器>"` 或 `click "text=按钮文字"` | 点击；自动跟随 `target=_blank` 新标签页 |
| `type <选择器> "<文本>" [--enter]` | 在普通输入框输入（账号/密码框会被拦） |
| `press <键>` / `scroll [--dy 800]` / `back` / `forward` / `reload` | 键盘、滚动、历史 |
| `tabs [--switch N]` / `close-tab` | 标签页管理 |
| `probe <url>` | 只看文件元信息，不下载（下载前必用） |
| `download --url <url> --reason "<用途>" --user-approved` | 直链下载（推荐路径） |
| `download --click "<选择器>" --reason "<用途>" --user-approved` | 点击按钮触发下载（站点强制走按钮时用） |
| `screenshot [--full]` | 补一张截图 |
| `fill-login`（stdin JSON） | 用户提供凭据后填写并可选提交 |

`click` 失败时返回体会带 `links`（当前可点链接），据此换更精确的选择器重试，不要反复盲试同一个。

## 四、标准工作流

### A. 搜索并回答
1. `search "关键词" --limit 8`
2. 挑 1–3 个最相关的 `url`，逐个 `open`（先附图说明"正在看第 X 个结果"，并给出该页面标题/来源）
3. 用 `text` 读正文要点，需要更多内容时 `scroll` 再 `text`
4. 汇总回答，注明来源链接；把最终页面截图附上

### B. 下载文件（重点）
1. `links --filter pdf`（或 `--filter 下载/zip/xlsx`）在页面上找直链；找不到就 `click "text=下载"` 试探 —— 会被 `file_navigation` 拦下并**直接返回文件元信息**，这正好是报告给用户所需的数据。
2. `probe <url>` 补齐类型/大小/文件名。
3. **停下来向用户报告 + 请求许可**（模板见铁律 1），把当前页面截图一并发出。
4. 用户同意 → `download ... --user-approved`；返回 `download.path`、`size_human`、`sha256`。
5. 回报结果：文件名、大小、保存绝对路径、来源 URL、下载理由；附下载后页面截图。
6. 用户拒绝 → 不下载，把页面内容/截图给到用户即可。

### C. 需要登录的站点
1. `open` 后发现 `auth_required: true`
2. 发截图 + 按铁律 2 的措辞向用户索取凭据
3. 用户给了 → `fill-login`（stdin）
4. 登录后 `status` 确认，再继续原任务

## 五、异常处理

| 现象 | 处理 |
| --- | --- |
| `results` 为空 | 页面可能改版或有验证码：`snapshot` 看实际内容，换 `--engine baidu` 重试 |
| 命令超时 | 重启后台守护进程，再重试一次；仍失败则改用 `open <url>` 直连目标页 |
| 页面莫名变回 `about:blank` | 守护进程被回收了：按第一节用后台任务方式重启 `bws_server.py` |
| 返回 `502 Bad Gateway` | 系统代理拦截了本机回环请求：客户端已内置绕过，若仍出现请检查 `HTTP_PROXY` 并设置 `NO_PROXY=127.0.0.1` |
| 站点屏蔽无头浏览器 | `restart --headed` 用可见窗口，再重试 |
| `blocked: file_navigation` | 正常护栏：转成"探测 → 报告 → 许可 → download"流程 |
| 目标元素找不到 | 用 `links` / `snapshot` 校正选择器，或用 `text=` 文字定位 |
| 正文为空或只有骨架屏 | 先 `scroll --dy 1200` 触发懒加载，再 `text`；图片空白属正常（未加载的装饰图），不影响取文 |

## 六、禁止事项

- 禁止在未取得用户明确许可前调用 `download`（传 `--user-approved` 等同于声称已获许可，必须真实发生）。
- 禁止自行编造文件大小、类型、来源页面。
- 禁止把用户密码写进命令行、脚本、文件或回复正文；禁止把凭据复用到其它站点。
- 禁止用 `open --approved` 绕过审批流程。
- 禁止在未发截图的情况下宣称"操作完成"。
