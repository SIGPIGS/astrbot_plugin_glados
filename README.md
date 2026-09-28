# astrbot_plugin_glados

AstrBot 插件：每天定时执行 [GLaDOS](https://glados.cloud)（glados.cloud / railgun.info）签到，并按账户把成功/失败结果推送到指定会话。签到接口行为参考 [BreakFree003/Gladoscheckin](https://github.com/BreakFree003/Gladoscheckin)。

## 功能

- 每天在配置的整点自动对所有账户签到；
- 每个账户可独立配置「成功通知 UMO」与「失败通知 UMO」（留空则不通知）；
- 通知内容包含签到获得的积分、剩余天数与总积分；
- 失败时区分 Cookie 失效与反自动化拦截（code 4），并给出对应的修复提示；
- `/glados checkin` 手动触发一次全部账户的签到。

## 配置

### 通用配置

| 键 | 说明 | 默认 |
|---|---|---|
| `checkin_hour` | 每天几点签到（0-23，按 AstrBot 所在时区） | `9` |
| `user_agent` | 签到请求 User-Agent | macOS Chrome UA |
| `request_timeout` | 单个请求超时（秒） | `30` |

> GLaDOS 会比对「登录浏览器的平台」与「签到请求的平台」。若签到被判定为自动签到（code 4，"Automated check-in detected"），请在登录所用浏览器的控制台执行 `navigator.userAgent`，把完整值填到 `user_agent`。实测 Windows / Linux / iPhone UA 会被拦截，macOS UA 默认可用。

### 账户配置

`accounts` 是一个数组，每个元素：

| 键 | 说明 |
|---|---|
| `name` | 账户备注名（用于通知与日志，缺省为「账户N」） |
| `cookie` | 登录后从浏览器复制的完整 Cookie |
| `success_umo` | 签到成功（含当日已签到）时通知的会话 UMO，留空不通知 |
| `failure_umo` | 签到失败时通知的会话 UMO，留空不通知 |

Cookie 说明：

- `glados.cloud` 的会话字段是 `gld:sess` 与 `gld:sess.sig`；
- `railgun.info` 的会话字段是 `koa:sess` 与 `koa:sess.sig`；
- 只在一个站点注册时，复制该站点的 Cookie 即可；两个站点都有账号时，把两对 Cookie 用 `"; "` 拼接成一份。

UMO 获取方式：在目标会话中发送 `/sid`。

## 命令

| 命令 | 说明 |
|---|---|
| `/glados checkin` | 立即执行一次全部账户的签到，并回复汇总结果 |

## 开发

```bash
# 在 AstrBot 仓库的虚拟环境中运行测试
uv run --project /path/to/AstrBot pytest tests/ -q
```
