# B站财经博主聚合服务

自动抓取B站财经博主动态、视频、评论、弹幕，用AI生成结构化摘要，通过REST API提供给安卓APP。

## 技术栈

- Python + FastAPI
- MySQL（复用现有容器，加入mysql-net网络）
- Docker
- MiMo ASR + MiMo LLM

## 项目结构

```
bilibili-financial-agg/
├── docs/              # 设计文档
│   ├── DESIGN.md      # 核心设计
│   ├── API.md         # REST API设计
│   └── POC.md         # POC测试记录
├── config/
│   ├── config.yaml    # 实际配置
│   ├── config.example.yaml
│   └── .env.example   # 环境变量模板
├── src/               # 源代码
├── tests/             # 测试
└── scripts/           # 运维脚本
```

## 环境变量（.env）

| 变量 | 说明 |
|------|------|
| SESSDATA | B站cookie |
| XIAOMI_API_KEY | MiMo API密钥 |
| DB_USER | MySQL用户名 |
| DB_PASSWORD | MySQL密码 |
| BILI_UID | 关注同步使用的 B 站 UID |
| DB_HOST / DB_PORT / DB_NAME | 数据库地址、端口、库名 |
| AUTO_MIGRATE | 默认 false；生产环境先用迁移账号执行升级 |

API Key 存在 `api_users` 表中，通过下面的管理命令生成；`FIN_AGG_API_KEY` 已不使用。

## 快速开始

```bash
cp config/config.example.yaml config/config.yaml
cp config/.env.example .env
# 编辑 .env，配置真实数据库和上游凭据。数据库 fin_agg 及 mysql-net 必须已存在。
docker compose build
# 停止旧版服务，再用具备 CREATE/ALTER 权限的迁移账号执行升级。
docker compose stop fin-agg
docker compose run --rm --no-deps fin-agg python -m scripts.manage_db upgrade
# 仅首次安装需要；已有用户无需重建。命令输出的 Key 请妥善保存。
docker compose run --rm --no-deps fin-agg python -m scripts.manage_db create-user admin
# 运行时账号仅需对应表的 SELECT/INSERT/UPDATE/DELETE 权限。
docker compose up -d fin-agg
```

迁移支持原有数据库，补齐 `retry_count`、动态重试队列、日报补算队列和任务状态表，并将字幕列升级为 MySQL `LONGTEXT`。升级是幂等的；升级前备份数据库。运行时默认只验证结构，不要求建表权限。也可在明确允许自动升级的开发环境设置 `AUTO_MIGRATE=true`。

服务使用一个 Uvicorn worker；启动时通过 MySQL 命名锁阻止重复调度实例。所有手动、定时、补抓、补字幕任务共享互斥锁。忙时返回 HTTP 409，接受任务后可在设置页或 `/api/tasks/{id}` 查询运行结果。

在 APP 配置服务器地址和管理命令生成的 API Key，连接验证通过后添加博主。当前博主配置来自数据库；`config.yaml` 中旧的 `bloggers` 不作为采集数据源。

## 日报与历史数据

时间统一使用上海时区。默认日报窗口为前一天 22:00 至当天 22:00（左闭右开），22:10 的任务先补抓再汇总。成功采集或补字幕的内容会按发布时间将所属日报加入补算队列；失败汇总保留旧结果，后续任务重试。`digest_cron`、`fetch_cron_1`、`fetch_cron_2` 支持完整五段 cron；旧 `daily_cron` 作为兼容别名，建议迁移到示例中的新配置。

schema 7 起，新增博主或缺失水位线时只建立当时的起点，不自动补历史。正常增量只接纳近期窗口内的新视频（默认 7 天），2026-07-01 是任何入口都不能绕过的绝对下限。发现扫描完整且候选已持久化才更新覆盖进度，发现与模型分析进度分别记录；近期窗口跳过的历史缺口单独保留。

旧视频、旧失败、旧字段 NULL 不会迁成新任务。升级、重启、换模型/提示词、更新 Cookie 不触发全库重算。自动重试只读取明确登记的新任务，各阶段独立计次；历史补抓使用 `/api/fetch/backfill` 的明确日期上下界、固定名单和额度，批次不自动向历史扩展。`/api/transcripts/retry` 只恢复已登记任务；`/api/work/retry` 可以提交最多 50 个明确选定的历史失败 BVID。补抓可用 `max_videos`、`max_pages` 限定本次额度。任务状态和覆盖区间可在 `/api/work/status` 查看。

完整转写保存在 BVID/CID 和内容版本绑定的检查点中。每个 ASR 成功片段单独保存；缺片不能发布，后续失败复用已成功片段。公开转写和摘要作为同一版本成对发布，旧结果在新分析完成前保持可读。评论和弹幕分别记录获取状态，互动缺失不使正文失败。超过 8000 字符的转写按段分析后综合，尾部也纳入输入。

运行中单实例锁连接持续保活，任务持有不可变代次，关键写入在同一事务内校验所有权。每次 ASR/LLM 请求开始前再检查所有权；已发出的外部请求可能无法撤回，失锁后不继续下一段。

## 授权与主动告警

服务器独立检查 B 站授权，`valid / invalid / unknown` 分开记录。超时、412 和风控不是授权失效；首次检测就已失效也创建持久告警，同一事件去重、最多每天提醒，恢复后记录解除时间。发送失败留在 outbox，服务重启不会丢失；推送服务接收不等于手机实际收到。

单用户应用统一使用连接配置中的 API Key，通过 `X-API-Key` 验证所有业务和授权接口。1.1.3 起取消第二把管理密钥、输入框及生成命令，已有连接 KEY 保持不变。缺失、无效或停用的 KEY 在进入授权逻辑前返回 401；B 站授权服务暂时异常不会清掉正常的阅读连接。APP 授权流程使用 HTTPS 并绑定发起时的服务器地址，切换服务器后不会把原会话的 nonce 发往新服务器。升级会清理旧管理密钥文件和密钥库条目；连接凭据不进入 Android 云备份或设备迁移备份。详见 [统一鉴权说明](docs/MANAGEMENT_KEY.md)。

重新授权在服务器创建三分钟有效的新会话，用户用官方 B 站 APP 确认；候选账号、登录状态验证通过后原子更新 `/app/data/bili-credential.json`，无需重启，不读取官方 APP 私有数据，不依赖 ROOT。账号优先匹配配置的 `BILI_UID`，未配置时匹配已保存的服务器账号。同机相册扫码流程已通过真机验证，正式包可直接使用；独立验证包仍可用 `-PauthorizationPoc=true` 构建。

授权支持受信 SSO 一次性票据及原始 Set-Cookie 值。短期回调/候选检查点位于凭据目录的 `.login-checkpoints`，文件权限为 600，与原凭据版本及三分钟会话绑定，到期清理。临时失败重试同一会话；凭据文件已替换而数据库提交失败时，仅允许与本会话检查点完全匹配的候选补记完成状态。票据已消费且网络响应完全丢失时仍可能需要重新扫码。普通手机权限的验证结果见 [真机记录](docs/DEVICE_VALIDATION_20261016.md)。

即时通知使用可选的 FCM HTTP v1。服务账号 JSON 只放服务器的 `/app/data/firebase-service-account.json` 并设置 600 权限；路径可在 `push.service_account_file` 或 `FCM_SERVICE_ACCOUNT_FILE` 指定。APP 的公共 Firebase 客户端配置参考 `android/app/firebase.properties.example`，真实配置文件不入 Git，绝不能把服务账号私钥放入 APP。用户主动启用时正常申请通知权限；后台周期检查只作补漏，不能声称即时送达。必须从服务器发送真实测试通知并在手机点击确认，不能用本地弹窗替代验收。

`asr.auth_failure_policy` 默认 `continue`，保留既有摘要可用性并受批次/每日音频预算限制。只有推送配置完整、设备允许通知且实际确认过服务器测试通知时，`wait` 才能生效；否则不让新任务悄悄长期等待。凭据恢复只唤醒明确关联旧凭据、仍在原范围内的已登记阻塞任务，不升级历史 ASR。真实后台通知和同机授权未验收时应明确保留此边界。

## 测试

后端需要 Python 3.12+ 和 PATH 中的 ffmpeg：

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

测试使用内存 SQLite、模拟 B 站/模型接口，音频测试使用合成音频实际调用 ffmpeg，不调用收费模型。未安装 ffmpeg 时媒体测试会标记跳过。

安卓需要 JDK 17+ 和 Android SDK：

```bash
cd android
./gradlew :app:testDebugUnitTest :app:assembleDebug :app:assembleRelease
```

Windows 使用 `gradlew.bat`。JVM 测试通过本地 MockWebServer 验证认证、游标、缓存与并发请求，使用内存存储替身；它们不能代替真机 Room/DataStore 和界面测试。Release 产物需要配置签名后才能分发。

本轮修复范围、验收结果及部署注意事项见 [修复验收记录](docs/FIX_ACCEPTANCE.md)。
