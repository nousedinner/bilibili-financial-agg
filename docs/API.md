# REST API 设计 v4

## Base URL
```
https://***REMOVED***/fin-api/
```

## 鉴权
- GET：公开
- POST/DELETE：需 `X-API-Key` header

## 通用响应
```json
{"code": 0, "message": "ok", "data": {...}}
```

错误码：401未授权、404不存在、429频率限制、502上游错误

---

## 博主管理 🔒

### GET /api/bloggers
博主列表

### POST /api/bloggers 🔒
添加博主

### DELETE /api/bloggers/{mid} 🔒
移除博主

---

## 视频

### GET /api/videos
视频列表（时间倒序）

**参数：** `blogger`(MID)、`tag`、`page`(默认1)、`limit`(默认20)

**响应：**
```json
{"items": [...], "total": 100, "page": 1, "has_more": true}
```

### GET /api/videos/{bvid}
单视频完整分析（摘要+评论+弹幕）

---

## 动态

### GET /api/dynamics
图文动态列表

**参数：** `blogger`(MID)、`page`、`limit`

---

## 聚合

### GET /api/feed
最新时间线（视频+动态混合）

**参数：** `limit`(默认20)、`before`(时间戳分页)

### GET /api/daily
可用日期列表

**响应：**
```json
{"dates": ["2026-08-17", "2026-08-16", "..."]}
```

### GET /api/daily/{date}
某日汇总

**响应：**
```json
{
  "date": "2026-08-17",
  "bloggers": [{
    "mid": 2137589551,
    "name": "李大霄",
    "videos": [...],
    "dynamics": [...],
    "overall_sentiment": "bearish"
  }],
  "cross_analysis": {
    "overall_sentiment": "偏防守",
    "consensus": ["高估值风险"],
    "key_topics": ["美债", "中概股"]
  }
}
```

---

## 系统

### GET /api/health
健康检查（Docker healthcheck）

### GET /api/status
系统状态（博主数、视频数、cookie过期时间、上次运行、失败数等）

### POST /api/fetch/trigger 🔒
手动触发抓取

### POST /api/fetch/backfill 🔒
历史补抓（每次≤20条，缺mid则对所有博主补抓）
```json
{"since": "2026-07-01", "mid": 2137589551}
```
