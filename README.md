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

首次采集仅处理 `first_run_cap` 条最新内容；更早内容通过 `/api/fetch/backfill` 分批补抓。视频和动态分析失败都有持久化重试记录，默认自动重试最多 3 次；视频也可通过 `/api/transcripts/retry` 手动重试。队列和任务结果不会因服务重启被当成成功。

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
