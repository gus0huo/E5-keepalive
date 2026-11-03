# E5 KeepAlive

通过 GitHub Actions 定时调用 Microsoft Graph API，维持 Microsoft 365 E5 开发者订阅的"开发活跃度"。零服务器、零成本、凭证全托管于 GitHub Secrets，运行日志自动脱敏，调度时间表用一张表格配置。

> **免责声明**：本项目为社区工具，只能提高续订概率，不保证 100% 续期成功。使用本项目即代表你理解：自动化调用与微软开发者计划的"真实开发活动"要求存在差异，请勿在 E5 账号中存放不可替代的数据。本项目与 Microsoft 无任何关联。

## 功能特性

- **多 E5 账户**：一段 JSON 管理多个租户，单账户故障不影响其他账户
- **表格式调度配置**：`schedule.yml` 一张表配置波次 / 时间段(北京) / 每天总量区间 / 各波百分比 / 推送，北京时间自动换算 UTC
- **智能预算**：每天总量在 plan_min~plan_max 随机抽取，按波次百分比递减执行，末波跑完剩余，每天恰好完成总量
- **随机触发点**：每个时间段 8 个候选触发点每日抽签执行 1 个，触发点每周自动更换
- **按需推送**：指定波次运行后推送汇总（支持 Server酱 / Telegram / SMTP 邮件）
- **订阅体检**：推送附带订阅状态、席位用量、到期时间推算
- **行为友好**：写操作即建即删不留残留；429/5xx 自动退避；404/507 优雅跳过
- **日志脱敏**：UPN、GUID、密钥在日志中自动打码，开源发布无压力

## 工作原理

```
schedule.yml（表格配置）
   └─ tools/build_keepalive.py 生成调度（每周一自动重抽触发点）
        └─ GitHub Actions 定时触发 → 抽签（每波 8 选 1）
             └─ OAuth 2.0 client_credentials → 按用户均摊预算调用 Graph API
                  └─ 记账（按北京日期跨波累计）→ 汇总 → 推送
```

## 快速开始

### 一、注册 Azure 应用（每个 E5 租户做一次）

1. 用该租户的**管理员账号**（`xxx@xxxxx.onmicrosoft.com`）登录 [portal.azure.com](https://portal.azure.com)
2. 「应用注册」→「新注册」→ 名称随意 → 账户类型「**仅此组织目录中的账户**」→ 重定向 URI 留空
3. 记下 **应用程序(客户端) ID** 和 **目录(租户) ID**
4. 「证书和密码」→「新建客户端密码」→ 复制 **Value**（只显示一次）
5. 「API 权限」→「添加权限」→ Microsoft Graph → **应用程序权限**：

| 权限 | 用途 | 必需性 |
|---|---|---|
| User.Read.All | 读取用户列表 | 必需 |
| Mail.ReadWrite | 邮件读写 + 到期邮件扫描 | 必需 |
| Calendars.ReadWrite | 日历写操作 | 必需（可降为 Read） |
| Files.ReadWrite.All | OneDrive 写操作 | 必需（可降为 Read） |
| Group.Read.All / Notes.Read.All / Chat.ReadBasic.All | 只读接口 | 可选 |
| Organization.Read.All | 订阅状态查询 | 可选 |

6. 点击「**代表组织授予管理员同意**」，全部变成绿色勾

### 二、创建仓库并上传文件

```
.github/workflows/keepalive.yml   ← 初始占位（Rebuild 后自动替换）
.github/workflows/rebuild-schedule.yml
tools/build_keepalive.py
schedule.yml                      ← 维护者唯一要改的文件
main.py
requirements.txt
README.md / LICENSE / .gitignore
```

### 三、配置 Secret：`E5_ACCOUNTS`

Settings → Secrets and variables → Actions → **Secrets** → New repository secret：

```json
[
  {
    "name": "账户一",
    "tenant_id": "<目录租户ID>",
    "client_id": "<应用程序客户端ID>",
    "client_secret": "<客户端密码Value>",
    "users": ["user1@yourtenant.onmicrosoft.com", "user2@yourtenant.onmicrosoft.com"],
    "expiry_scan_user": "admin@yourtenant.onmicrosoft.com"
  }
]
```

> 存储前先在 [jsonlint.com](https://jsonlint.com) 校验。每项之间有逗号，最后一项后面没有逗号。`daily_calls` 字段已失效，每天总量统一由 `schedule.yml` 管理。

### 四、（可选）通知渠道

| 渠道 | Secret 名 | 获取方式 |
|---|---|---|
| Server酱（微信） | `NOTIFY_SERVERCHAN_KEY` | [sct.ftqq.com](https://sct.ftqq.com) 获取 SendKey |
| Telegram | `NOTIFY_TELEGRAM_BOT_TOKEN` / `NOTIFY_TELEGRAM_CHAT_ID` | @BotFather 建 bot |
| SMTP 邮件 | `NOTIFY_SMTP_HOST` / `NOTIFY_SMTP_USER` / `NOTIFY_SMTP_PASS` / `NOTIFY_SMTP_TO` | 邮箱开启 SMTP 生成授权码 |

### 五、生成调度并试运行

1. Settings → Actions → General → Actions permissions 选「Allow all actions...」
2. Actions → **Rebuild Schedule** → Run workflow（生成正式调度并自动提交）
3. Actions → **E5 KeepAlive** → Run workflow 测试
4. 预期日志：`今日目标抽定：XX 次（范围 70-90）` → `执行完毕`

## 配置参考

### schedule.yml（运行配置表）

| 字段 | 说明 | 示例 |
|---|---|---|
| `time` | 时间段（北京，24h，支持跨零点） | `"05:00-07:00"` |
| `plan_min` / `plan_max` | 每天每账户请求总数区间，每天抽一个 | `70` / `90` |
| `percent` | 本波完成【当天总量】的百分比，末波必须 `100` | `"30-50"` |
| `notify` | 本波跑完是否推送 | `false` |
| `candidate_points` | 每时间段候选触发点数 | `8` |
| `rotate` | 触发点更换周期 | `weekly` |

改完 `schedule.yml` 后运行 Actions → **Rebuild Schedule** 生效。

### `E5_ACCOUNTS` 字段

| 字段 | 说明 | 默认 |
|---|---|---|
| `name` | 账户别名 | 账户N |
| `tenant_id` / `client_id` / `client_secret` | Azure 应用三件套 | 必填 |
| `users` | 参与调用的 UPN 列表；`[]` 为租户内前 25 个 | `[]` |
| `enable_writes` | 是否执行可回滚写操作 | `true` |
| `expiry` | 手动指定到期日 | 空 |
| `expiry_scan_user` | 扫描哪个邮箱推算到期日 | 第一个 user |

## 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `invalid_client` / 401 | Secret 粘错或粘了「密码 ID」 | 重建 Secret |
| 403 / Authorization_RequestDenied | 权限未授予管理员同意 | 补应用权限 + 管理员同意 |
| JSON 不合法 | 缺逗号 / 中文引号 | jsonlint 校验 |
| `mysite not found` / 507 | 用户未开通 OneDrive / 存储满 | 正常跳过 |
| `IndentationError` | 粘贴缩进错乱 | 整文件替换 |
| 配置错误提示 `[配置错误]` | schedule.yml 字段填错 | 按提示改对应行 |
| 定时没跑 | GitHub 延迟 / 长期无提交被暂停 | Rebuild Schedule 每周自动提交，可忽略 |
| Node.js 20 deprecated 警告 | 平台提示 | 无害 |

**额度**：每天约 24 次轻量触发（多数几秒退出）+ 3 次真跑，月耗约 900 分钟，免费额度 2000 分钟/月。

## 安全说明

- 凭证只存于 GitHub Secrets，仓库中零明文；fork 者无法获取
- 运行日志自动脱敏（UPN / GUID / 密码打码）
- 转公开前：删除历史运行记录、轮换客户端密码、提交邮箱设为私有
- 建议每 1~2 年轮换客户端密码；定期核对开发者计划资料页剩余天数
- 请遵守 GitHub Actions 与 Microsoft 服务条款，本项目仅供学习研究

## License

MIT
