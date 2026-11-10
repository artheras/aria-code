# Aria Code 统一账户与三端记忆

日期：2026-10-22。状态：本地实现，尚未发布到生产或更新已安装 CLI。

## 交付行为

`/login` 默认打开 `https://arthera.xyz/cli-login`，不再需要 ARIA_LOGIN_PAGE 开关，也不再默认绕过账户页面直接选 Google。网页与桌面端复用同一个 AccountAuthPage，支持登录、注册、当前账户确认、使用其他账户、邮箱验证/重发、密码恢复及失败重试。CLI 使用 loopback 回调、随机 state 与 S256 PKCE 兑换 Firebase 会话。

所有服务以 Firebase 项目 arthera-66e5b 的 UID 作为账户标识，不凭相同邮箱合并账户。CLI 保存经过交接的 UID、实际邮箱及真实认证来源，邮箱更新保持同一个记忆目录。浏览器不可用的邮箱/密码备用路径也使用 Firebase 的同一个邮箱认证提供方，移除旧 `/auth/login` 及“邮箱充当 user_id”的回退。

## 域名及凭据

登录页、默认 API、模型网关和支持邮箱均更新为 arthera.xyz。旧保存的生产 API 地址自动迁移；云端记忆客户端还会拦截旧域名配置，避免向已过期的 arthera.finance 发送账户令牌。自托管和 staging 的显式地址保持可配置。

按 [Firebase Auth REST API](https://firebase.google.com/docs/reference/rest/auth#section-refresh-token) 使用 refresh token 续期 Firebase ID token。刷新前后核对项目、UID 和当前会话；旧账户的晚返回不能覆盖新登录账户。续期失败时保留本地数据，提示重新登录/重试，不伪造同步成功。配置及记忆文件使用 owner-only 权限。

## 三端记忆实际链路

Code 对接与 Web、桌面相同的 `/api/v2/memory/preferences`、`/summary`、`/items`。当前账户的昵称、职业、个人描述及 durable/preference 事实会进入 Code 的模型上下文；新捕获的标准个人事实先写入当前账户的本地 outbox，然后上传到同一云端 UID。项目文件、代码、会话历史和旧无归属文件不自动上传。

每次对话开始时刷新凭据并同步记忆，普通同步间隔至少 60 秒。记忆网络同步最多等待两秒，较慢的同步在后台继续，本轮使用已有缓存；没有缓存时不会编造记忆。显式 `/memory sync` 可立即尝试同步。断网或服务故障保留 outbox；服务器按操作 client_id 原子创建，重试不会重复创建或恢复已删除记录。操作 ID 保持到重试完成，用户明确重新添加同一内容会产生新操作。

Code 的删除会排队同步至云端；Web/桌面删除后，Code 下次同步会替换快照，而非从旧本地副本恢复该事实。记忆开关在同步时从云端读取，关闭后停止个人记忆注入和新增上传，显式删除仍可执行。关闭状态已读到但后续 summary 请求失败，也立即停用已有个人记忆。上传过程中被用户取消的事实保持隐藏，并排队删除，避免晚返回重新显示。

本地目录是 `<ARIA_HOME>/accounts/<SHA-256(UID)>/`，匿名使用独立分区。旧 `memory/` 和根目录 ARIA.md 原样保留，不猜测其所属账户。账户切换/退出清空当前对话与响应上下文，再读取对应分区；不删除另一账户数据。个人“记住”请求进入账户记忆，避免把个人偏好自动写入公共项目 ARIA.md。

多进程使用文件锁合并 outbox，并通过 owner-only 临时文件和原子替换更新快照，避免同时打开两个 Code 窗口导致待同步记录丢失。

## 使用

- `/login`：打开统一网页认证。
- `/whoami`：显示账户与邮箱，不输出令牌片段。
- `/memory status`：显示当前账户、记忆开关、待同步数量和真实同步状态。
- `/memory account`：查看当前账户记忆及 ID。
- `/memory account add 偏好简洁的中文回答`：新增账户记忆。
- `/memory sync`：立即同步。
- `/memory forget <ID>`：标记删除，下次同步提交至云端。

原 `/memory show|add` 的项目 ARIA.md 操作保留；`/memory global` 现在也使用当前账户的记忆分区。匿名使用、敏感内容过滤及本地项目资料仍保持独立。

## 验证与发布

自动化覆盖了 A→B→A、退出、邮箱变化、错误 UID、旧请求晚返回、令牌刷新、离线重试、响应丢失、Web 删除/开关、删除与上传竞态、多进程 outbox、统一登录路由、loopback/state 校验及既有 CLI/模型运行流程。网络交互均使用测试服务/桩；没有使用真实用户凭据进行线上登录、发送邮件或读写真实云端记忆。

网页认证在实际浏览器检查桌面和 390px 窄屏，无横向溢出。代码通过相关测试、Ruff、网页生产构建和 Python wheel 构建。截图保存在 Arthera/output/design/aria-code-unified-auth.png。

发布顺序：先发布记忆 API 的 client_id 幂等写入及账户认证接口，再发布网站统一认证路由，最后发布 Aria Code 和桌面新包。桌面包仍按现有签名/公证流程发布。源代码和本地 wheel 完成不代表线上网站或用户已安装的软件已自动更新；正式发布后还需验收真实账户、真实邮件及跨设备记忆读写。
