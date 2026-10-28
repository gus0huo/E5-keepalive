# E5 KeepAlive (AutoApiSecret style)

通过定时调用 Microsoft Graph API 维持 Microsoft 365 E5 开发者订阅活跃度。
凭证只存放在 GitHub Secrets，采用 client_credentials 流程，密钥可随时撤销。

## 一、注册 Azure 应用（用 E5 管理员账号）

1. Azure 门户 -> 「应用注册」->「新注册」，名称随意，账户类型选「仅此组织目录」
2. 记下「应用程序(客户端) ID」和「目录(租户) ID」
3. 「证书和密码」->「新建客户端密码」，立即复制生成的 Secret Value（只显示一次）
4. 「API 权限」->「添加权限」-> Microsoft Graph -> **应用程序权限**，勾选：
   - User.Read.All
   - Mail.ReadWrite（若关闭写操作可降为 Mail.Read）
   - Calendars.ReadWrite（可降为 Calendars.Read）
   - Files.ReadWrite.All（可降为 Files.Read.All）
   - Contacts.Read、Group.Read.All、Notes.Read.All、Chat.ReadBasic.All
5. 点击「代表组织授予管理员同意」，状态全部变为绿色勾

## 二、配置 GitHub 仓库

1. 建议创建**私有仓库**并推送本项目
2. Settings -> Secrets and variables -> Actions -> New repository secret：
   - AZURE_TENANT_ID  = 目录(租户) ID
   - AZURE_CLIENT_ID  = 应用程序(客户端) ID
   - AZURE_CLIENT_SECRET = 上面复制的密码值
3. Settings -> Actions -> General -> Actions permissions 选「允许」
4. Actions -> E5 KeepAlive -> Run workflow，看日志中 Test / 成功计数

## 三、可选变量（Settings -> Secrets and variables -> Actions -> Variables）

| 变量 | 说明 | 默认 |
|---|---|---|
| TARGET_USERS | 只跑指定用户，逗号分隔 UPN | 租户内前 25 个用户 |
| MIN_CALLS / MAX_CALLS | 每次随机动作数区间 | 18 / 36 |
| ENABLE_WRITES | 是否执行可回滚写操作 | true |

## 注意

- 本项目只提升续订概率，不保证 100% 续期
- 客户端密码会过期，记得定期轮换并更新 Secret
- 不要在 E5 账号中存放不可替代的数据
