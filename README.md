# E5 KeepAlive

通过 GitHub Actions 定时调用 Microsoft Graph API，维持 Microsoft 365 E5 开发者订阅的"开发活跃度"。零服务器、零成本、凭证全托管于 GitHub Secrets，运行日志自动脱敏。

> **免责声明**：本项目为社区工具，只能提高续订概率，不保证 100% 续期成功。使用本项目即代表你理解：自动化调用与微软开发者计划的"真实开发活动"要求存在差异，请勿在 E5 账号中存放不可替代的数据。本项目与 Microsoft 无任何关联。

## 功能特性

- **多 E5 账户**：一段 JSON 管理多个租户，单账户故障不影响其他账户
- **智能预算**：每账户每天恰好 N 次请求，支持固定值（`80`）或区间随机（`"70-90"`，当天抽定、多波共用）
- **随机运行时间**：上午 / 下午两个时间窗内随机抽签执行，避免机械规律
- **按需推送**：每天仅下午场运行后推送汇总（支持 Server酱 / Telegram / SMTP 邮件）
- **订阅体检**：推送附带订阅状态、席位用量、到期时间推算
- **行为友好**：写操作（草稿/日历/文件夹）即建即删不留残留；429/5xx 自动退避；404/507 优雅跳过
- **日志脱敏**：UPN、GUID、密钥在日志中自动打码，开源发布无压力

## 工作原理

```
GitHub Actions（定时抽签）
   └─ OAuth 2.0 client_credentials 获取 token
        └─ 按用户均摊预算，随机调用 Microsoft Graph API（读 + 可回滚写）
             └─ 记账（每日预算累计）→ 汇总 → 推送通知
```

## 快速开始

### 一、注册 Azure 应用（每个 E5 租户做一次）

1. 用该租户的**管理员账号**（`xxx@xxxxx.onmicrosoft.com`）登录 [portal.azure.com](https://portal.azure.com)
2. 搜索「应用注册」→「新注册」→ 名称随意 → 账户类型选「**仅此组织目录中的账户**」→ 重定向 URI 留空
3. 记下概览页的 **应用程序(客户端) ID** 和 **目录(租户) ID**
4. 「证书和密码」→「新建客户端密码」→ 立即复制生成的 **Value**（只显示一次）
5. 「API 权限」→「添加权限」→ Microsoft Graph → **应用程序权限**，勾选：

| 权限 | 用途 | 必需性 |
|---|---|---|
| User.Read.All | 读取用户列表 | 必需 |
| Mail.ReadWrite | 邮件读写 + 到期邮件扫描 | 必需 |
| Calendars.ReadWrite | 日历写操作 | 必需（可降为 Read） |
| Files.ReadWrite.All | OneDrive 写操作 | 必需（可降为 Read） |
| Group.Read.All / Notes.Read.All / Chat.ReadBasic.All | 只读接口 | 可选 |
| Organization.Read.All | 订阅状态查询 | 可选 |

6. 点击「**代表组织授予管理员同意**」，全部变成绿色勾

### 二、创建仓库

新建一个**私有**仓库（跑完稳定后可转公开，见「开源发布」），放入 4 个文件：

```
.github/workflows/keepalive.yml   ← 工作流
main.py                           ← 主程序
requirements.txt                  ← 依赖（仅 requests）
README.md
```

### 三、配置 Secret：`E5_ACCOUNTS`

Settings → Secrets and variables → Actions → **Secrets** → New repository secret：

- Name：`E5_ACCOUNTS`
- Value：

```json
[
  {
    "name": "账户一",
    "tenant_id": "<目录租户ID>",
    "client_id": "<应用程序客户端ID>",
    "client_secret": "<客户端密码Value>",
    "users": ["user1@yourtenant.onmicrosoft.com", "user2@yourtenant.onmicrosoft.com"],
    "daily_calls": "70-90",
    "expiry_scan_user": "admin@yourtenant.onmicrosoft.com"
  },
  {
    "name": "账户二",
    "tenant_id": "<第二个租户的ID>",
    "client_id": "<第二个应用的ID>",
    "client_secret": "<第二个密码Value>",
    "users": []
  }
]
```

> 存储前建议先在 [jsonlint.com](https://jsonlint.com) 校验语法。注意每项之间要有逗号，最后一项后面没有逗号。

### 四、（可选）通知渠道

三选一即可，全部留空则仅在 GitHub Summary 查看结果：

| 渠道 | Secret 名 | 获取方式 |
|---|---|---|
| Server酱（微信） | `NOTIFY_SERVERCHAN_KEY` | [sct.ftqq.com](https://sct.ftqq.com) 登录获取 SendKey |
| Telegram | `NOTIFY_TELEGRAM_BOT_TOKEN` / `NOTIFY_TELEGRAM_CHAT_ID` | @BotFather 建 bot |
| SMTP 邮件 | `NOTIFY_SMTP_HOST` / `NOTIFY_SMTP_USER` / `NOTIFY_SMTP_PASS` / `NOTIFY_SMTP_TO` | 邮箱开启 SMTP 并生成授权码 |

### 五、启用并试运行

1. Settings → Actions → General → Actions permissions 选「Allow all actions...」
2. Actions → E5 KeepAlive → **Run workflow**
3. 预期日志：`共 N 个 E5 账户` → `今日目标抽定：XX 次（范围 70-90）` → `执行完毕`

## 配置参考

### `E5_ACCOUNTS` 字段

| 字段 | 说明 | 默认 |
|---|---|---|
| `name` | 账户别名（出现在日志和推送） | 账户N |
| `tenant_id` / `client_id` / `client_secret` | Azure 应用三件套 | 必填 |
| `users` | 参与调用的用户 UPN 列表；`[]` 为租户内前 25 个用户 | `[]` |
| `daily_calls` | 每天总请求数：`80` 或 `"70-90"` | `80` |
| `runs_per_day` | 每天跑几波（仅影响分摊提示） | `2` |
| `enable_writes` | 是否执行可回滚写操作 | `true` |
| `expiry` | 手动指定到期日，如 `"2027-01-20"` | 空 |
| `expiry_scan_user` | 扫描哪个邮箱推算到期日（建议管理员 UPN） | 第一个 user |

### GitHub Variables（可选，非 Secret）

| 变量 | 说明 |
|---|---|
| `DAILY_CALLS` / `RUNS_PER_DAY` | 全局默认预算 / 波数 |
| `MIN_CALLS` / `MAX_CALLS` | `daily_calls: 0` 时的旧随机模式 |
| `ENABLE_WRITES` | 全局写开关 |
| `REDACT_LOGS` | 设为 `false` 关闭日志脱敏（不建议） |

> ⚠️ Variables 的值会明文打印在运行日志里，真实 UPN 请只放 `E5_ACCOUNTS`（Secret）。

## 运行节奏

- 上午窗 10:00-12:00、下午窗 15:00-17:00（北京时间），每窗 8 个候选触发点，当日抽签选 1 个真正执行，其余几秒退出
- 上午场静默运行，**下午场运行完推送**当日汇总
- 每日预算跨波累计：上午跑剩多少下午补多少，确保每天恰好 N 次

## 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `invalid_client` / 401 | Secret 值粘错，或粘了「密码 ID」而不是「值」 | 重建 Secret |
| 403 / Authorization_RequestDenied | 权限未授予管理员同意，或勾成了「委托的权限」 | 补「应用程序权限」+ 管理员同意 |
| `E5_ACCOUNTS 不是合法 JSON` | 缺逗号 / 中文引号 / 多余逗号 | jsonlint 校验后重贴 |
| `User's mysite not found` / 507 | 该用户未开通 OneDrive / 存储已满 | 正常跳过，无需处理 |
| `IndentationError` | 粘贴时缩进错乱 | 整文件替换重贴 |
| 定时没跑 | GitHub 延迟 / 长期无提交被暂停 | Actions 页面重新启用 |
| Node.js 20 deprecated 警告 | GitHub 平台提示 | 无害，可忽略 |

**额度**：每天约 16 次轻量触发（多数几秒退出）+ 2 次真跑，月耗约 500~600 分钟，私有仓库免费额度 2000 分钟/月。

## 安全说明

- 凭证只存于 GitHub Secrets，仓库中零明文；fork 者无法获取
- 运行日志自动脱敏（UPN / GUID / 密码打码），开源发布不暴露账号
- 建议每 1~2 年轮换一次客户端密码；定期核对 [开发者计划资料页](https://developer.microsoft.com/microsoft-365/dev-program) 的剩余天数
- 请遵守 GitHub Actions 与 Microsoft 服务条款，本项目仅供学习研究

## License

MIT
