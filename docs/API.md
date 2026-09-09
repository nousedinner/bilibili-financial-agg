# REST API 当前契约

除 `GET /api/health` 外，业务接口均需要 `X-API-Key`。`POST /api/bootstrap` 保持关闭；首次用户通过 `python -m scripts.manage_db create-user 用户名` 创建。

成功响应为 `{"code":0,"data":...}`；验证失败使用 HTTP 400/422，认证失败 401/403，资源不存在 404，任务冲突 409，上游 B 站故障 502，健康检查失败 503。错误正文包含 `detail`，无需客户端解析错误正文来识别认证失败。

## Feed 与视频

`GET /api/feed` 参数：`limit`（1–100，默认 20）、`blogger`（MID）、`sentiment`（bullish/bearish/neutral）、`before`（Unix 秒）、`before_id`（复合 ID）。按发布时间和类型前缀 ID 倒序，过滤禁用博主。

```json
{
  "code": 0,
  "data": {
    "items": [{"type":"dynamic","dyn_id":"123","mid":456,"publish_time":"2026-09-09T12:00:00","summary":"...","sentiment":"neutral"}],
    "total": 25,
    "has_more": true,
    "next_cursor": {"before":1788926400,"before_id":"dynamic:123"}
  }
}
```

下一页原样携带 `next_cursor` 中的两个字段。ID 使用 `video:BV...` 或 `dynamic:数字`，避免混合类型冲突。不能从最后一条时间自行构造游标。仅时间的旧游标仍兼容，但无法保证同秒边界完整；`cursor` 作为旧字段别名保留，安卓读取 `next_cursor`。`total` 为筛选后的总数，`has_more` 表示本页之后是否还有数据。

`GET /api/videos` 支持 `blogger`、`tag`、`sentiment`、`page`（从 1 开始）、`limit`；筛选在计数和分页前执行。`GET /api/videos/{bvid}` 返回视频元数据及 `summary`、`transcript`、`comments`、`danmaku`。评论的 `total` 是上游总数，弹幕的 `total` 是所获取 XML 的完整条数，`sampled` 是实际采样条数，二者不混用。

`GET /api/dynamics` 为图文列表，支持 `blogger`、`page`、`limit`。响应中的无时区时间字符串均按 Asia/Shanghai 解释。

## 博主管理

- `GET /api/bloggers`：启用博主列表；`tags` 始终为数组。
- `POST /api/bloggers?name=...&mid=...&tags=...`：添加或重新启用，MID 可省略以搜索用户名；姓名 1–100 字，MID 为正整数。
- `DELETE /api/bloggers/{mid}`：禁用。返回 `data.message`。
- `POST /api/bloggers/sync`：完整分页获取当前 B 站关注，返回 `total_followings`、`already_tracked`、`new_count`、`new`。任何中途请求失败均报错，不把部分列表当完整结果；禁用博主会重新出现在待添加列表。
- `POST /api/bloggers/sync/add`：建议发送对象数组，最多 500 项，姓名可包含逗号。

```json
[{"mid":123,"name":"甲"},{"mid":456,"name":"乙,乙"}]
```

兼容旧 `mids`/`names` 查询参数，但名字数量必须与 MID 数量匹配，空名字只在原位置补默认值。成功返回 `data.added`。

## 日报

`GET /api/daily` 返回 `data.dates`（日期字符串数组）。`GET /api/daily/{YYYY-MM-DD}` 返回平铺对象：`date`、`summary`、`bloggers`（姓名数组）、`overall_sentiment`、`overall_sentiment_desc`、`sentiment_score`、`consensus`、`differences`、`key_topics`。日期占位不代表已加载的中性分析；客户端需单独获取详情。

日报默认 22:00 截止，成功补抓历史内容后会补算所属日期；模型失败不会覆盖已有日报。

## 任务与状态

- `POST /api/fetch/trigger`：抓取并处理历史日报补算队列。
- `POST /api/fetch/backfill`，JSON `{"since":"2026-07-01","mid":123}`：补抓；MID 可省略，使用数据库启用列表。日期在接受任务前校验。
- `POST /api/transcripts/retry`：补字幕及完整重分析；字幕、摘要、评论/弹幕分析一起提交。

以上入口共享任务锁，忙时返回 HTTP 409。接受任务不表示采集完成：

```json
{"code":0,"data":{"id":"任务 UUID","status":"accepted","message":"任务已接受，可在状态页查看结果"}}
```

`GET /api/tasks/{id}` 返回 `kind`、`status`、`started_at`、`finished_at`、`result`、`error`。状态为 running/completed/partial/failed/interrupted。服务启动会把中断的任务及 pending 视频恢复为可识别的失败状态。

`GET /api/status` 返回博主/视频/失败数、`last_fetch`（最近任务完成时间或 null）、`last_job`、`task_running` 和 cookie 状态。Cookie 检查在数据库会话关闭后执行，缓存 60 秒。

`GET /api/health` 检查数据库连接及已启动的调度器；不可用时返回 HTTP 503，供 Docker 健康检查使用。

## 用户

`GET /api/users`、`POST /api/users`（JSON `{"username":"..."}`，1–50 字）和 `DELETE /api/users/{id}` 需要有效 Key。当前权限模型仍是所有有效 Key 拥有相同权限；本轮没有引入新的角色系统。
