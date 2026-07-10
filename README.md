# Wahoo 骑行数据 → Garmin Connect 国区 自动同步

利用 Wahoo 的"电子邮件活动"功能，将每次骑行产生的 `.fit` 文件自动同步到**佳明中国区**账户。

## 工作原理

```
Wahoo ELEMNT 骑行结束
       ↓
Wahoo 自动发送含 .fit 附件的邮件
       ↓
GitHub Actions 定时运行（每 15 分钟）
       ↓
Python 脚本通过 IMAP 连接邮箱
       ↓
搜索 Wahoo 邮件 → 提取 .fit 附件
       ↓
登录 Garmin Connect 国区 (connect.garmin.cn)
       ↓
上传 .fit 文件到佳明国区账户
       ↓
提交去重缓存到仓库，避免重复上传
```

## 前置条件

1. **Wahoo 电子邮件活动已开启**：在 Wahoo ELEMNT App 中 → Settings → Email Notifications → 勾选"Email FIT file"，填入你的 QQ 邮箱地址
2. **QQ 邮箱已开启 IMAP 服务**：设置 → 账户 → 开启 IMAP/SMTP 服务，获取授权码
3. **Garmin Connect 国区账户**：确认你的佳明账户属于国区（网址以 `connect.garmin.cn` 结尾）
4. **GitHub 账户**：免费版即可，Actions 免费额度足够

## 部署步骤

### 1. Fork 或创建仓库

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
| `IMAP_SERVER` | `imap.qq.com` | QQ 邮箱 IMAP 服务器地址 |
| `IMAP_USER` | `your_email@qq.com` | 接收 Wahoo 邮件的 QQ 邮箱地址 |
| `IMAP_PASSWORD` | `your_qq_authorization_code` | QQ 邮箱 IMAP 授权码（非登录密码） |
| `GARMIN_EMAIL` | `your_garmin@email.com` | 佳明国区账户邮箱 |
| `GARMIN_PASSWORD` | `your_garmin_password` | 佳明国区账户密码 |
| `WAHOO_SENDER` | `wahooligan.com` | （可选）Wahoo 发件人域名，实际为 `wahooligan.com` |
| `MAIL_FOLDER` | `INBOX` | （可选）邮箱搜索文件夹 |

### 3. QQ 邮箱授权码获取方式

QQ 邮箱的 IMAP 密码不是 QQ 登录密码，而是需要单独生成的**授权码**：

1. 登录 QQ 邮箱网页版 → **设置** → **账户**
2. 找到"POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务"
3. 开启 **IMAP/SMTP 服务**（如果已开启，先关闭再重新开启以获取新授权码）
4. 按提示用手机发送短信验证
5. 验证成功后会得到一个 **16 位授权码**（如 `abcdefghijklmnop`）
6. 将此授权码填入 GitHub Secret `IMAP_PASSWORD`

> 如果使用其他邮箱，IMAP 服务器地址参考：Gmail `imap.gmail.com`、Outlook `outlook.office365.com`，端口均为 `993`

### 4. 启用 GitHub Actions

- 进入仓库 **Actions** 页面
- 如果提示需要确认，点击 **"I understand my workflows, go ahead and enable them"**
- 工作流将按照设定的 cron 自动运行

### 5. 手动测试

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
| 账户体系 | 独立 | 独立（与国区不互通） |
| 代码配置 | `is_cn=False`（默认） | `is_cn=True` |

脚本中通过 `Garmin(..., is_cn=True)` 确保连接到**佳明国区**服务器，而非国际区。如果你的佳明账户在国际区，将 `is_cn=True` 改为 `is_cn=False` 即可。

## 去重机制

- 每次运行后，已处理邮件的指纹（邮件 ID + 日期的 hash）会保存到 `data/processed_emails.json`
- 该文件通过 git commit 回写到仓库，下次运行时自动加载
- 上传失败的邮件不会被标记为已处理，下次运行会自动重试

## 限制和注意事项

1. **GitHub Actions schedule 有延迟**：实际触发时间可能比 cron 设定晚 5-30 分钟，高峰时段更长。如果对实时性要求高，可缩短间隔或手动触发
2. **佳明登录认证**：佳明可能在某些情况下要求验证码（MFA）。目前脚本在无 MFA 的账户上可正常自动登录。如果账户开启了 MFA，需要额外处理
3. **仓库需保持 Public 或 Actions 可用**：GitHub 免费版 Private 仓库每月有 2000 分钟 Actions 额度，Public 仓库免费
4. **邮箱空间**：Wahoo 邮件含 .fit 附件，每封约几十 KB，长期使用不会造成问题
5. **API 变更风险**：garminconnect 库依赖佳明的非官方 API，佳明可能随时变更认证流程导致失效

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
- Python 标准库 `imaplib` / `email` — IMAP 邮件处理

## 故障排查

| 问题 | 解决方案 |
|---|---|
| IMAP 登录失败 | 检查 IMAP_SERVER/PORT/USER/PASSWORD，确认邮箱已开启 IMAP |
| Garmin 登录失败 | 确认账户在国区（`connect.garmin.cn`），检查密码是否正确 |
| 没有找到 Wahoo 邮件 | 确认 Wahoo App 中已开启 Email FIT file 功能；检查 WAHOO_SENDER 配置 |
| 上传失败 409 | 重复上传，已自动处理为跳过 |
| Actions 没有运行 | 确认仓库 Actions 已启用；schedule 任务需仓库有近期提交才会运行 |
| 去重缓存丢失 | 确保 workflow 中的 `git commit` 步骤执行成功，检查 `GITHUB_TOKEN` 权限 |

## 许可

MIT
