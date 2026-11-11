# Padlet 图片批量发帖工具（固定访客昵称）

一个 Playwright 脚本：打开指定的 Padlet 板，以**同一个固定的访客昵称**，向随机选中的 Column 发布带图片的帖子。图片和 Column 随机，昵称不随机、整个运行期间只设置一次。

## 设计边界

- **单一固定身份**：启动时把 Guest Alias 设为 `--alias`（或 `names.txt` 第一行），全程不再修改。没有多昵称轮换。
- **不绕过任何限制**：遇到验证码 / `Security check` / 登录墙 / `429` 等信号立即停止，不重试、不规避。
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
| `--interval` | 帖子间隔秒数（最低 5 秒） |
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
- 失败时 `error_<时间>_postN.png/.html`

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

这是 Padlet 端的上传失败（瞬时错误或短时间上传过多），不是图片格式问题（同样的图稍后重试可成功）。脚本现在的行为：

1. 上传阶段检测到该提示 → 先等旧提示消失，再**自动重试，最多 3 次**（递增等待；此时尚未提交，不会产生重复帖子）；
2. 仍失败 → 记 `failed`、立即停止、保存截图与 DOM 到 `logs/`。

实测该提示是间歇性的：同一张图稍后重试即可成功，网络层可见文件其实已 201 上传、预览也生成了，属于 Padlet 端的偶发错误。

建议：两次上传间隔不要太小（默认 5 秒）；若怀疑限流，等几分钟再跑。

**日志出现 `Target page, context or browser has been closed`**

运行期间浏览器窗口被关闭（手动关闭或崩溃）。脚本会把它记为失败并停止。请让窗口保持打开直到程序结束。

## 测试

```bash
.venv/bin/pytest -q
```

测试用本地 mock 板跑通完整流程（列发现、昵称设置、图片上传、发布、校验、dry-run、安全停止、uncertain 停止），**不触及 padlet.com**。

## 已验证 / 未验证

已验证（真实板子，2026-11-11）：

- 访客昵称修改与回读校验
- 4 个 Column 的发现与标题读取
- 打开编辑器、通过 Upload 的文件选择器上传图片、预览渲染
- 关闭编辑器（dry-run 不提交）
- **真实站点发布 1 条**（Section 2，作者 `TestBot`），并以「目标 Section 内出现昵称 + 图片」判据回读为 `True`
- 本地 mock 板上的完整发布与校验流程（11 个测试）

实现中修正的一个真实问题：首次真实发布时，React 重建 DOM 丢弃了临时标记，且发布后的图片 URL 与预览 URL 不同，曾导致误报 `uncertain`（脚本按设计停止、未重复提交）。现已改为按 Section 标题 + 昵称 + 图片定位，并在每次发帖前重新扫描 Column。

限制：

- 发布成功判据依赖「该 Section 中出现昵称文本与图片」。若你的板子开启匿名发帖且不显示昵称，会退化为「页面帖子数增加」；两者都不成立时记为 `uncertain` 并停止，不会重复提交。
- 真实站点由 React 渲染；已通过「每次发帖前重新扫描 Column」缓解标记丢失问题。
- Padlet 若启用 Cloudflare 人机校验，脚本会按设计直接停止，需要手动完成校验后重跑。
