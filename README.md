# Padlet 图片批量发帖工具（固定访客昵称）

一个 Playwright 脚本：打开指定的 Padlet 板，以**同一个固定的访客昵称**，向随机选中的 Column 发布带图片的帖子。图片和 Column 随机，昵称不随机、整个运行期间只设置一次。

## 设计边界

- **单一固定身份**：启动时把 Guest Alias 设为 `--alias`（或 `names.txt` 第一行），全程不再修改。没有多昵称轮换。
- **不绕过任何限制**：遇到验证码 / `Cloudflare challenge` / `Security check` 时，脚本**暂停并保持浏览器窗口打开**，等你在窗口里手动完成验证后自动继续；不关闭窗口、不尝试绕过。只有等你超过 `--security-timeout` 仍未完成时才会停止。
- **不重复提交**：发布结果无法确认时记为 `uncertain` 并**停止**，避免产生重复帖子。
- 串行执行，单浏览器实例，帖子之间默认至少 5 秒间隔。

## 安装

```bash
cd padlet_bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

## 使用

交互式：

```bash
.venv/bin/python main.py
# Padlet URL: https://padlet.com/...
# Number of posts: 5
```

命令行：

```bash
.venv/bin/python main.py \
  --url "https://padlet.com/<your-workspace>/<your-board-id>" \
  --count 5
```

| 参数 | 说明 |
| --- | --- |
| `--url` | 目标板 URL |
| `--count` | 发布数量；>20 需再次确认（`--yes` 跳过） |
| `--alias` | 固定访客昵称（优先于 `names.txt`） |
| `--headless` | 无界面运行（默认显示浏览器） |
| `--dry-run` | 只打开编辑器并上传图片，**不点 Publish** |
| `--diagnose` | 只抓取页面诊断信息，不发帖 |
| `--upload-debug` | 单次上传诊断：上传一张图、**不发布、不重试**，保存网络日志/截图/DOM |
| `--image` | 配合 `--upload-debug` 指定图片（默认从 `images/` 随机） |
| `--upload-timeout` | 诊断时观察一次上传的秒数（默认 90；超时记 `UNKNOWN`） |
| `--interval` | 帖子间隔秒数（最低 5 秒） |
| `--security-timeout` | 安全验证最多等你多少秒（默认 `0`＝一直等，窗口保持打开） |
| `--verbose` | 调试日志 |

## 目录

```
padlet_bot/
├── main.py
├── requirements.txt
├── names.txt          # 第一行作为固定昵称
├── selectors.json     # 可选：覆盖内置选择器
├── images/            # jpg / jpeg / png / webp
├── logs/              # 运行日志、CSV、截图、诊断文件
└── tests/
    ├── mock_padlet.html
    ├── security.html
    ├── conftest.py
    └── test_pipeline.py
```

运行后在 `logs/` 得到：

- `run_<时间>.log`：时间戳、URL、序号、昵称、图片、Column、状态、错误
- `posts_<时间>.csv`：结构化结果
- `upload_network_<时间>.jsonl`：每次运行的网络事件（见下）
- 失败时 `error_<时间>_postN.png/.html`

### 上传诊断 `--upload-debug`

```bash
.venv/bin/python main.py --url "<你的板子>" --upload-debug \
  --image "images/某张图.png"
```

只上传一张图，**不点 Submit、不自动重试**，并把当下状态（截图、DOM、网络日志）全部留下。终端与 `logs/upload_debug_<时间>.txt` 给出：

```
Upload selected: image001.png
Upload request: POST https://uploads-usc1.padletcdn.com/...
Upload response: 201 ... / PUT 200 ...
Image processing: 200 https://padlet.com/beethoven-v2/file
Image loading: 200 https://v1.padlet.pics/1/image.webp
Final status: READY | FAILED | UNKNOWN
Reason: ...
```

状态机为 `SELECTED → UPLOADING → SERVER_ACCEPTED → PROCESSING → READY`，任一步失败即 `FAILED`；证据不足（超时、只看到本地 blob 预览、没观察到上传请求）则记 `UNKNOWN`，**不会假定成功**。

网络日志把「传输」「后处理」「图片加载」分开记录：

| 阶段 | 请求 | 判定 |
| --- | --- | --- |
| 文件传输 | `POST/PUT uploads-*.padletcdn.com` | 4xx/5xx 或 `requestfailed` |
| 图片后处理 | `POST padlet.com/beethoven-v2/file` | 4xx/5xx 或 `requestfailed` |
| 图片加载 | `GET v1.padlet.pics/...` | 4xx/5xx 或 `requestfailed` |

安全：URL 只记录 `scheme://host/path`（丢弃全部查询参数），`Cookie` / `Authorization` / `Set-Cookie` 等请求头一律写 `[redacted]`，不记录请求体（上传体就是图片本身），也不会记录任何凭证。

> 常见误解：进度条到 80% 不等于「文件没传上去」。Padlet 的流程是先把字节 PUT 到 CDN，再调用 `beethoven-v2/file` 让服务器做缩放/转换，最后加载处理后的图片。80% 附近失败通常发生在**后处理**或**图片加载**，而不是传输本身。诊断日志能明确区分是哪一步。

## 发布状态

| 状态 | 含义 |
| --- | --- |
| `published` | 编辑器关闭且目标 Column 的帖子数确实增加 |
| `pending_moderation` | 页面出现待审核文案 |
| `failed` | 出现错误提示，或编辑器未关闭 |
| `uncertain` | 编辑器关闭但无法证明帖子已创建（**停止运行**） |
| `dry_run` | 仅上传，未发布 |

## 选择器（已在真实板子验证）

内置选择器于 2026-11-11 在目标板上实测：

| 元素 | 选择器 | 状态 |
| --- | --- | --- |
| 访客头像 | `button[aria-label="Go to sign up page"]` | 已验证 |
| 菜单项 | 按钮文字 `Change name` | 已验证 |
| 昵称输入 | `input[type=text]`（对话框内） | 已验证 |
| 昵称保存 | 按钮 `Done` | 已验证 |
| 昵称回读验证 | 重新打开 `Change name` 读取输入框 | 已验证（保存后回读为 `TestBot`） |
| Column | `button[aria-label="Add post to this section"]` | 已验证（4 个 Section） |
| 主题框 | `textarea[aria-label="Post subject"]` | 已验证 |
| 正文框 | `[role=textbox][aria-label="Post body"]` | 已验证 |
| 上传 | `button[aria-label="Upload"]` → file chooser | 已验证（预览渲染） |
| 提交 | `button[aria-label="Submit post"]` | 已验证（真实发布 1 条） |
| 发布校验 | 目标 Section 内出现昵称 + 图片，或帖子卡片数增加 | 已验证（对已发布帖子回读为 True） |

首次在新的板子上运行，仍建议：

```bash
.venv/bin/python main.py --url "<你的板子>" --diagnose
```

`logs/diagnose_*.png/.html/.txt` 会列出每个选择器是否命中及页面所有可见可交互元素。若某项未命中，把真实选择器写入 `selectors.json`，结构见 `main.py` 的 `DEFAULT_SELECTORS`：

```json
{
  "publish_button": [
    {"kind": "role", "role": "button", "name": "^Publish$"}
  ]
}
```

## 常见问题

**Padlet 提示 `Sorry, there's a error when uploading the file`**

先不要靠「多重试几次」判断原因，直接跑单次诊断：

```bash
.venv/bin/python main.py --url "<你的板子>" --upload-debug --image "images/出问题的那张.png"
```

它会给出这次上传在**哪个阶段**失败（传输 / 后处理 / 图片加载）、HTTP 状态或 `requestfailed` 原因，并保留截图与 DOM。

脚本现在的判定规则：

1. **成功**必须同时满足：文件传输 2xx、服务器端后处理 2xx、预览是服务器 URL 且已渲染。只看到本地预览或提示消失都不算成功。
2. 仅当失败是 **5xx / 网络错误 / 上传器未就绪** 时才重试（最多 3 次；此时尚未 Submit，不会产生重复帖子）。
3. 遇到 **401 / 429**（权限、限流）立即 `StopRun`，不重试、不绕过。文件本身的 4xx（如 400/413/415）也不重试。
4. **403 要区分两种情况**：
   - 响应头带 `cf-mitigated: challenge` 的 403 是 **Cloudflare 人机验证**，不是权限拒绝。脚本会打印提示、保持窗口打开、等你手动完成验证，然后最多自动重试 3 次；期间绝不让浏览器退出。
   - 普通 403（没有 challenge 头）仍按权限错误 `StopRun`。
5. 重试仍失败 → 记 `failed`、停止、保存截图与 DOM。

如果诊断显示「传输 2xx + 后处理失败」，说明文件已经传上去了，问题在 Padlet 的图像处理环节；日志里请保留 `beethoven-v2/file` 的状态码和响应体（已做敏感信息过滤），可据此向 Padlet 反馈或稍后重试。若显示「完全没观察到上传请求」，则是上传控件尚未就绪，属于客户端时序问题，重试是安全的。

**日志出现 `Target page, context or browser has been closed`**

运行期间浏览器窗口被关闭（手动关闭或崩溃）。脚本会把它记为失败并停止。请让窗口保持打开直到程序结束。

## 测试

```bash
.venv/bin/pytest -q
```

测试用本地 mock 板跑通完整流程（列发现、昵称设置、图片上传、发布、校验、dry-run、安全停止、uncertain 停止），**不触及 padlet.com**。共 25 个测试。

## 已验证 / 未验证

已验证（真实板子，2026-11-11）：

- 访客昵称修改与回读校验
- 4 个 Column 的发现与标题读取
- 打开编辑器、通过 Upload 的文件选择器上传图片、预览渲染
- 关闭编辑器（dry-run 不提交）
- **真实站点发布 1 条**（Section 2，作者 `TestBot`），并以「目标 Section 内出现昵称 + 图片」判据回读为 `True`
- 本地 mock 板上的完整发布与校验流程，以及上传诊断的网络脱敏 / 4xx-vs-requestfailed / 状态机测试（25 个测试）
- 安全验证暂停逻辑：验证窗口出现时脚本保持等待，验证消失后自动继续（`test_security_check_waits_for_the_user`）

实现中修正的一个真实问题：首次真实发布时，React 重建 DOM 丢弃了临时标记，且发布后的图片 URL 与预览 URL 不同，曾导致误报 `uncertain`（脚本按设计停止、未重复提交）。现已改为按 Section 标题 + 昵称 + 图片定位，并在每次发帖前重新扫描 Column。

限制：

- 发布成功判据依赖「该 Section 中出现昵称文本与图片」。若你的板子开启匿名发帖且不显示昵称，会退化为「页面帖子数增加」；两者都不成立时记为 `uncertain` 并停止，不会重复提交。
- 真实站点由 React 渲染；已通过「每次发帖前重新扫描 Column」缓解标记丢失问题。
- Padlet 若启用 Cloudflare 人机校验，脚本会暂停等待你在窗口中完成验证（默认不设超时），完成后自动继续；不会因为验证码直接关闭页面。
