# Wahoo 骑行数据 → Garmin Connect 国区 自动同步

利用 Wahoo 的"电子邮件活动"功能，将每次骑行产生的 `.fit` 文件自动同步到**佳明中国区**账户。

## 工作原理

```
Wahoo ELEMNT 骑行结束
       ↓
Wahoo 自动发送活动通知邮件（含网页下载链接，无附件）
       ↓
GitHub Actions 定时运行（每 15 分钟）
       ↓
Python 脚本通过 IMAP 连接邮箱
       ↓
搜索 Wahoo 邮件 → 提取 workout_summaries 链接中的活动 ID
       ↓
HTTP 模拟登录 wahooligan.com（SAML SSO）
       ↓
访问活动页面 → 下载 .fit 文件
       ↓
登录 Garmin Connect 国区 (connect.garmin.cn)
       ↓
上传 .fit 文件到佳明国区账户
       ↓
提交去重缓存到仓库，避免重复上传
```

> **重要**：Wahoo 的活动邮件**不包含 .fit 附件**，只包含一个网页链接（如 `wahooligan.com/workout_summaries/422997785`）。需要登录 Wahoo 账户后才能下载 `.fit` 文件。本脚本通过 HTTP 模拟登录 Wahoo 网站来自动完成下载。

## 前置条件

1. **Wahoo 电子邮件活动已开启**：在 Wahoo ELEMNT App 中 → Settings → Email Notifications → 勾选"Email FIT file"，填入你的邮箱地址
2. **Wahoo 账户未开启 MFA**：本脚本使用账号密码模拟登录，不支持双重验证。如果开启了 MFA，请在 wahooligan.com 的 Account Settings 中关闭
3. **邮箱已开启 IMAP 服务**（以 QQ 邮箱为例）：设置 → 账户 → 开启 IMAP/SMTP 服务，获取授权码
4. **Garmin Connect 国区账户**：确认你的佳明账户属于国区（网址以 `connect.garmin.cn` 结尾）
5. **GitHub 账户**：免费版即可，Actions 免费额度足够

## 部署步骤

### 1. 创建仓库

将本仓库的以下文件复制到你的 GitHub 仓库：

```
sync_wahoo_to_garmin.py          # 主同步脚本
requirements.txt                # Python 依赖
.github/workflows/sync.yml       # GitHub Actions 工作流
```

### 2. 配置 GitHub Secrets

在仓库页面 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，依次添加：

| Secret 名称 | 值 | 说明 |
|---|---|---|
| `IMAP_SERVER` | `imap.qq.com` | 邮箱 IMAP 服务器地址 |
| `IMAP_USER` | `your_email@qq.com` | 接收 Wahoo 邮件的邮箱地址 |
| `IMAP_PASSWORD` | `your_authorization_code` | 邮箱 IMAP 授权码（非登录密码） |
| `WAHOO_EMAIL` | `your_wahoo@email.com` | Wahoo 账户邮箱 |
| `WAHOO_PASSWORD` | `your_wahoo_password` | Wahoo 账户密码 |
| `GARMIN_EMAIL` | `your_garmin@email.com` | 佳明国区账户邮箱 |
| `GARMIN_PASSWORD` | `your_garmin_password` | 佳明国区账户密码 |
| `WAHOO_SENDER` | `wahooligan.com` | （可选）Wahoo 发件人域名 |
| `MAIL_FOLDER` | `INBOX` | （可选）邮箱搜索文件夹 |

### 3. QQ 邮箱授权码获取方式

QQ 邮箱的 IMAP 密码不是 QQ 登录密码，而是需要单独生成的**授权码**：

1. 登录 QQ 邮箱网页版 → **设置** → **账户**
2. 找到"POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务"
3. 开启 **IMAP/SMTP 服务**（如果已开启，先关闭再重新开启以获取新授权码）
4. 按提示用手机发送短信验证
5. 验证成功后会得到一个 **16 位授权码**
6. 将此授权码填入 GitHub Secret `IMAP_PASSWORD`

> 如果使用其他邮箱，IMAP 服务器地址参考：Gmail `imap.gmail.com`、Outlook `outlook.office365.com`，端口均为 `993`

### 4. 确认 Wahoo 账户信息

1. 访问 [wahooligan.com/login](https://wahooligan.com/login)
2. 用邮箱和密码登录，确认能正常进入
3. 确认 **未开启 Two-Factor Authentication**（Account Settings → Security 中无 2FA 选项）
4. 登录后访问一个活动页面（如 `wahooligan.com/workout_summaries/xxx`），确认能看到"Download"按钮

### 5. 启用 GitHub Actions

- 进入仓库 **Actions** 页面
- 如果提示需要确认，点击 **"I understand my workflows, go ahead and enable them"**
- 工作流将按照设定的 cron 自动运行

### 6. 手动测试

在 Actions 页面：
1. 选择 **Wahoo to Garmin CN Sync** 工作流
2. 点击 **Run workflow** 手动触发一次
3. 查看运行日志，确认无报错

## 调整运行频率

编辑 `.github/workflows/sync.yml` 中的 cron 表达式：

```yaml
schedule:
  # 每 15 分钟（默认）
  - cron: '*/15 * * * *'
  # 每 30 分钟
  # - cron: '*/30 * * * *'
  # 每 6 小时
  # - cron: '0 0,6,12,18 * * *'
```

> **注意**：GitHub Actions 对 schedule 任务有延迟（通常 5-15 分钟），且对长时间空闲的仓库会降低调度频率。如果仓库超过 60 天没有提交，schedule 任务会被自动暂停。

## 关于佳明国区与国际区的区别

这是本项目的关键设计点：

| 项目 | 国际区 | 国区（本项目） |
|---|---|---|
| 域名 | `garmin.com` | `garmin.cn` |
| Connect 网址 | `connect.garmin.com` | `connect.garmin.cn` |
| 账户体系 | 独立 | 独立（与国际区不互通） |
| 代码配置 | `is_cn=False`（默认） | `is_cn=True` |

脚本中通过 `Garmin(..., is_cn=True)` 确保连接到**佳明国区**服务器，而非国际区。如果你的佳明账户在国际区，将 `is_cn=True` 改为 `is_cn=False` 即可。

## 去重机制

- 每次运行后，已处理的活动 ID 会保存到 `data/processed_workouts.json`
- 该文件通过 git commit 回写到仓库，下次运行时自动加载
- 上传失败的不会被标记为已处理，下次运行会自动重试

## 下载策略

脚本从 Wahoo 活动页面下载 `.fit` 文件时，会依次尝试以下策略：

1. **HTML 链接扫描**：在页面中查找包含 `.fit` 的 `<a>` 标签
2. **下载/导出按钮**：查找 download/export 相关的链接或按钮
3. **HTML 属性**：在按钮的 `data-*` 属性中查找 `.fit` URL
4. **JavaScript 代码**：在 `<script>` 标签中正则匹配 `.fit` URL
5. **常见 URL 模式探测**：对候选 URL 发 HEAD 请求，检查 Content-Type

如果所有策略都失败，会将页面源码保存为 `debug_{workout_id}.html` 供调试。

## 限制和注意事项

1. **Wahoo 登录依赖**：脚本模拟 Wahoo SAML SSO 登录，Wahoo 可能随时变更认证流程导致失效
2. **不支持 MFA**：如果 Wahoo 账户开启了双重验证，脚本无法自动登录
3. **GitHub Actions schedule 有延迟**：实际触发时间可能比 cron 设定晚 5-30 分钟
4. **佳明登录认证**：佳明可能在某些情况下要求验证码（MFA）。目前脚本在无 MFA 的账户上可正常自动登录
5. **仓库需保持活跃**：GitHub 免费版 Private 仓库每月有 2000 分钟 Actions 额度，Public 仓库免费；超过 60 天无提交的仓库 schedule 任务会被暂停
6. **API 变更风险**：garminconnect 库依赖佳明的非官方 API，佳明可能随时变更认证流程导致失效

## 项目结构

```
wahoo-garmin-sync/
├── sync_wahoo_to_garmin.py       # 主同步脚本
├── requirements.txt              # Python 依赖
├── .github/
│   └── workflows/
│       └── sync.yml               # GitHub Actions 工作流
├── .gitignore
└── README.md
```

## 依赖

- `garminconnect` — Garmin Connect API 封装库，支持国区域名 (`is_cn=True`)
- `requests` — HTTP 请求库，用于模拟 Wahoo 登录和下载文件
- `beautifulsoup4` — HTML 解析库，用于从页面提取下载链接
- Python 标准库 `imaplib` / `email` — IMAP 邮件处理

## 故障排查

| 问题 | 解决方案 |
|---|---|
| IMAP 登录失败 | 检查 IMAP_SERVER/USER/PASSWORD，确认邮箱已开启 IMAP |
| Wahoo 登录失败 | 确认 WAHOO_EMAIL/WAHOO_PASSWORD 正确，且未开启 MFA |
| .fit 下载失败 | 查看日志中是否保存了 debug HTML，Wahoo 页面结构可能已变更 |
| Garmin 登录失败 | 确认账户在国区（`connect.garmin.cn`），检查密码是否正确 |
| 没有找到 Wahoo 邮件 | 确认 Wahoo App 中已开启 Email FIT file 功能；检查 WAHOO_SENDER 配置 |
| 上传失败 409 | 重复上传，已自动处理为跳过 |
| Actions 没有运行 | 确认仓库 Actions 已启用；schedule 任务需仓库有近期提交才会运行 |
| 去重缓存丢失 | 确保 workflow 中的 `git commit` 步骤执行成功，检查 `GITHUB_TOKEN` 权限 |

## 许可

MIT
