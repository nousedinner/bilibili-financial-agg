# 服务器部署与维护

## 准备

需要 Docker Compose、已建立的 `mysql-net` 网络，以及该网络中可通过主机名 `mysql` 访问的 MySQL 服务。提前创建 `fin_agg` 数据库，字符集使用 `utf8mb4`；本项目的 Compose 不负责安装 MySQL。

在仓库根目录执行：

```bash
cp config/config.example.yaml config/config.yaml
cp config/.env.example .env
chmod 600 .env config/config.yaml
```

填写 `.env`，示例占位值不能直接使用：

| 变量 | 用途 |
| --- | --- |
| `XIAOMI_API_KEY` | MiMo 模型服务密钥 |
| `DB_USER` / `DB_PASSWORD` / `DB_NAME` | 数据库账号、密码和库名 |
| `DB_HOST` / `DB_PORT` | Compose 中固定为 `mysql:3306`；直接运行时可覆盖 |
| `BILI_UID` | 服务器允许授权的 B 站账号 UID；不填时按已保存会话的账号校验 |
| `SESSDATA` | 可选的初始 B 站 Cookie；也可留空后通过 APP 扫码授权 |
| `AUTO_MIGRATE` | 生产环境保持 `false`，启动前单独执行迁移 |

`config/config.yaml` 用于调整采集计划、模型、音频预算和请求间隔。默认北京时间 12:00、21:00 采集，22:10 生成日报，日报截止时间为 22:00。博主列表由 APP 管理并存入数据库；模板中的旧 `bloggers` 字段不作为采集来源。

## 首次启动

先在 `.env` 中配置有建表及变更权限的迁移账号：

```bash
docker compose build
docker compose run --rm --no-deps fin-agg python -m scripts.manage_db upgrade
docker compose run --rm --no-deps fin-agg python -m scripts.manage_db create-user admin
```

最后一条命令只在首次安装时执行，它会输出 APP 使用的连接 KEY，请保存到密码管理器。已有账号时命令拒绝覆盖原 KEY；升级无需重新生成。这个 KEY 用来访问自己的后端，B 站扫码则用来授权服务器访问 B 站，两者用途不同。

迁移完成后，将 `.env` 改为运行账号，对业务数据库授予 `SELECT`、`INSERT`、`UPDATE`、`DELETE` 权限，再启动：

```bash
docker compose up -d fin-agg
docker compose ps
curl --fail http://127.0.0.1:8091/api/health
```

服务只绑定本机 `127.0.0.1:8091`。手机连接应通过 HTTPS 反向代理访问；如部署到 URL 子路径，代理需正确转发 API 路径。接口文档在服务的 `/docs`，不需要维护另一份静态接口列表。

## 持久化、备份与升级

MySQL 保存业务数据、任务状态和告警；Compose 的命名卷 `fin-agg-data` 保存 B 站会话、登录检查点和本地 outbox。`./config` 以只读方式挂载到 `/app/config`。实际卷名可能带 Compose 项目前缀，以容器挂载信息为准：

```bash
docker inspect fin-agg --format '{{json .Mounts}}'
```

升级前停止采集服务，备份业务数据库、实际数据卷、`.env`、`config/config.yaml`，并记录当前镜像和源码版本。数据库备份使用有备份权限的账号及私有客户端配置文件，不把密码写入命令历史。

```bash
docker compose stop fin-agg
# 完成上述备份后，更新源码和构建镜像。
docker compose build
# 临时使用迁移账号执行，完成后换回运行账号。
docker compose run --rm --no-deps fin-agg python -m scripts.manage_db upgrade
docker compose up -d fin-agg
curl --fail http://127.0.0.1:8091/api/health
```

迁移是幂等的，当前结构版本为 8。版本 8 将连接 KEY 转为 SHA-256 摘要，原有手机 KEY 继续有效；数据库不再保存可直接使用的原始 KEY。保留单实例和单 worker；多个实例会被数据库所有权锁阻止。不要使用 `docker compose down -v` 清理运行中的持久化数据。

1.1.5 与 1.1.4 使用相同结构。迁移工具可为任意现有模型表补加无默认值、无索引或约束的可空列；需要回填、非空值、外键或索引的变更必须编写显式迁移，不能靠启动自动猜测。

回滚应使用已记录的兼容镜像，并核对其与当前数据库结构及鉴权方式是否兼容。需要恢复数据库时，先停服务并备份现状，避免旧备份覆盖升级后产生的业务数据。

结构版本 8 不能直接搭配版本 7 的旧镜像使用。回退优先使用兼容摘要鉴权的镜像；如需恢复旧鉴权，应从升级前的私有备份定点恢复 `api_users` 和结构版本记录，保留升级后产生的业务数据。

## 采集与恢复规则

- 默认自动窗口为最近 7 天，绝对下限为 2026-07-01。新增博主或缺失水位线时建立当前起点，不自动翻历史。
- 历史记录及空阶段字段不会自动变成待处理任务。更新模型、提示词、Cookie 或重启服务不重算旧视频。
- `/api/fetch/backfill` 需明确日期上下界，并通过 `max_videos`、`max_pages` 限制批次；批次重启不扩大范围。
- `/api/work/retry` 接收明确选择的失败 BVID，最多 50 个。`/api/transcripts/retry` 只处理已经登记的任务。
- `/api/work/status` 查看阶段状态与发现覆盖；`/api/tasks/{id}` 查看已提交任务。HTTP 409 表示已有任务在执行。
- 不完整转写不发布摘要。成功 ASR 片段可复用，超过 60 分钟的视频跳过 ASR；评论或弹幕缺失会分别记录。
- 有样本时模型漏返回情绪字段会记为分析失败；无样本时 API 返回 `no_samples`，不发布估算情绪比例。旧缓存的互动结果必须通过当前版本校验。
- 无新版校验标记且为 100% 中性的历史互动结果来源不明，详情接口返回 `unverified` 和空分析对象，保留库中数据。该指纹不能证明模型漏字段，不据此自动重跑历史视频。
- 正文分析和互动分析分别最多启动三次；输入变化和手动重新接纳不清零这些计数，成功检查点仍可复用。耗尽后停止自动重试。
- 任务终态暂存文件每分钟由当前所有者补写一次，采集任务运行期间延后；SQLite 访问在工作线程中串行执行。待补写或本地文件不可读时健康检查返回 503；启动恢复失去所有权时服务保持降级，由所有权监控重试恢复。
- 音频秒数和 LLM 调用次数按尝试预留，用来约束支出，并非服务商账单；超时或响应丢失时可能已产生费用，因此不自动退还该次额度。
- 扫码会话创建间隔至少 10 秒，同时最多保留 3 个有效待确认会话；过期记录定期回收。

## 授权与通知

在 APP 中重新授权时，由服务器创建短期二维码，官方 B 站 APP 确认后，后端校验账号与登录状态，再原子替换 `/app/data/bili-credential.json`。文件权限为 `600`，无需重启；已有会话优先于 `.env` 中的初始 Cookie。

服务器独立每 30 分钟检查授权，采集过程中也会记录明确失效证据。状态分为 `valid`、`invalid`、`unknown`；超时、412 和风控不会被当作登录过期。失效、提醒及恢复事件持久化、去重，最多每天提醒一次。重新授权仅恢复关联旧凭据版本且仍在原范围内的阻塞任务。

默认由 APP 启动检查及后台周期检查读取事件并显示通知。FCM 是可选通道，未配置不影响这种方式，也不保证即时或熄屏送达。若以后配置 FCM，APP 公共配置参考 `android/app/firebase.properties.example`，服务账号私钥只放服务器数据卷，文件权限设为 `600`，路径由 `push.service_account_file` 或 `FCM_SERVICE_ACCOUNT_FILE` 指定。

`asr.auth_failure_policy` 保持 `continue`，无 AI 字幕时按预算降级到 ASR。只有真实服务器推送在手机上收到并点击确认后，配置为 `wait` 才能生效。

## 故障检查

```bash
docker compose ps
docker compose logs --tail=100 fin-agg
curl --fail http://127.0.0.1:8091/api/health
```

启动时提示数据库结构不匹配：检查连接的库，并用迁移账号执行 `upgrade`。APP 返回 401：核对连接 KEY 和账号启用状态；无需因此重复 B 站扫码。授权显示 `invalid`：在 APP 中重新授权；显示 `unknown`：先检查网络或风控。采集处于冷却或预算耗尽时查看任务状态，不要通过重置全库失败次数或批量重建历史任务恢复。
