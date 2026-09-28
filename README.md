# astrbot_plugin_glados

AstrBot 插件：每天定时执行 [GLaDOS](https://glados.cloud)（glados.cloud / railgun.info）签到，并按账户把成功/失败结果推送到指定会话。签到接口行为参考 [BreakFree003/Gladoscheckin](https://github.com/BreakFree003/Gladoscheckin)。

## 功能

- 每天在配置的整点自动对所有账户签到；
- 每个账户可独立配置 Cookie、User-Agent 与「签到通知 UMO」（留空则不通知）；
- 通知内容包含签到获得的积分、剩余天数与总积分；
- 失败时区分 Cookie 失效与反自动化拦截（code 4），并给出对应的修复提示；
- `/glados checkin` 手动签到绑定到当前会话的账户。

## 配置

### 通用配置

| 键 | 说明 | 默认 |
|---|---|---|
| `checkin_hour` | 每天几点签到（0-23，按 AstrBot 所在时区） | `9` |
| `request_timeout` | 单个请求超时（秒） | `30` |

### 账户配置

`accounts` 是一个数组，每个元素：

| 键 | 说明 |
|---|---|
| `name` | 账户备注名（用于通知与日志，缺省为「账户N」） |
| `cookie` | 登录后从浏览器复制的完整 Cookie |
| `user_agent` | 该账户的 User-Agent，留空使用内置默认（见下方说明） |
| `notify_umo` | 签到通知 UMO：该账户的签到结果（成功与失败）都推送到这个会话，留空不通知 |

> **为什么每个账户要单独配置 `user_agent`**：GLaDOS 会校验「签到请求的平台」与「登录该账户时浏览器的平台」是否一致，不一致时签到会被拒绝（code 4，"Automated check-in detected"）。不同账户可能在不同设备/浏览器上登录，因此每个账户都应填写**登录时所用浏览器**控制台中 `navigator.userAgent` 的完整值；留空则使用内置默认（macOS Chrome UA，实测 Windows / Linux / iPhone UA 会被拦截）。

Cookie 说明：

- `glados.cloud` 的会话字段是 `gld:sess` 与 `gld:sess.sig`；
- `railgun.info` 的会话字段是 `koa:sess` 与 `koa:sess.sig`；
- 只在一个站点注册时，复制该站点的 Cookie 即可；两个站点都有账号时，把两对 Cookie 用 `"; "` 拼接成一份。

UMO 获取方式：在目标会话中发送 `/sid`。

## 命令

| 命令 | 说明 |
|---|---|
| `/glados checkin` | 立即签到「签到通知 UMO」绑定到**当前会话**的账户，并回复这些账户的汇总结果 |

> 手动签到按会话隔离：只有 `notify_umo` 等于当前会话 UMO 的账户会被执行，会话中也不会看到其他账户的结果。定时签到仍然覆盖全部账户。

## 开发

```bash
# 在 AstrBot 仓库的虚拟环境中运行测试
uv run --project /path/to/AstrBot pytest tests/ -q
```
