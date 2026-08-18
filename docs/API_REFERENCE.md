# 财经聚合API接口文档

> Base URL: `https://***REMOVED***/fin-api/api`
> 响应格式: JSON，`{"code": 0, "data": {...}}` 表示成功

---

## 认证方式

数据库用户表认证。创建用户时自动生成密码（长随机串），客户端请求时在Header中携带：

```
X-API-Key: <密码>
```

- 除 `health` 和 `bootstrap` 外，所有接口均需认证
- 认证失败返回 `401 {"detail": "无效的API Key"}`

---

## 1. 系统

### GET /api/health
健康检查，无需认证。
```json
{"status": "ok"}
```

### GET /api/status 🔒
系统状态概览。
```json
{
  "code": 0,
  "data": {
    "bloggers": 1,
    "videos": 44,
    "failed": 0,
    "last_fetch": "2026-08-18T03:00:00",
    "cookie_valid": true,
    "cookie_expire": ""
  }
}
```

---

## 2. 用户管理

### POST /api/bootstrap
首次创建用户（仅当无用户时可用），无需认证。
```json
{"username": "android"}
→ {"code": 0, "data": {"username": "android", "api_key": "自动生成的密码"}}
```

### POST /api/users 🔒
创建新用户。
```json
{"username": "ios"}
→ {"code": 0, "data": {"username": "ios", "api_key": "自动生成的密码"}}
```

### GET /api/users 🔒
查看用户列表。

### DELETE /api/users/{user_id} 🔒
删除用户。

---

## 3. 博主管理

### GET /api/bloggers 🔒
博主列表。
```json
{
  "code": 0,
  "data": [
    {
      "mid": 2137589551,
      "name": "李大霄",
      "tags": ["价值投资", "A股"],
      "enabled": true,
      "added_at": "2026-08-17 17:25:50"
    }
  ]
}
```

### POST /api/bloggers 🔒
添加博主。
- 参数: `?mid=xxx&name=yyy&tags=财经,投资`
- mid: B站用户数字ID（从B站个人主页URL获取）

### DELETE /api/bloggers/{mid} 🔒
删除博主。

---

## 4. 视频数据

### GET /api/videos 🔒
视频列表，支持筛选和分页。

参数:
| 参数 | 类型 | 说明 |
|------|------|------|
| page | int | 页码，默认1 |
| limit | int | 每页数量，默认20 |
| blogger | int | 按博主mid筛选 |
| sentiment | string | 按情绪筛选: bullish/bearish/neutral |

```json
{
  "code": 0,
  "data": {
    "items": [
      {
        "bvid": "BV1VEba6HEvN",
        "mid": 2137589551,
        "title": "华尔街投资人再度买入中概股",
        "duration": 220,
        "publish_time": "2026-08-17T15:25:12",
        "view_count": 63622,
        "sentiment": "neutral",
        "sentiment_score": 0.2,
        "summary": "视频讨论华尔街传奇投资人德鲁肯米勒买入中概股..."
      }
    ],
    "total": 44,
    "page": 1,
    "has_more": true
  }
}
```

### GET /api/videos/{bvid} 🔒
单视频完整分析详情。
```json
{
  "code": 0,
  "data": {
    "bvid": "BV1VEba6HEvN",
    "title": "华尔街投资人再度买入中概股",
    "duration": 220,
    "publish_time": "2026-08-17T15:25:12",
    "view_count": 63622,
    "content_type": "video",
    "transcript": {
      "source": "ai_subtitle",
      "text": "（字幕全文）..."
    },
    "summary": {
      "summary": "视频讨论华尔街传奇投资人德鲁肯米勒买入中概股...",
      "key_points": ["德鲁肯米勒打破做空中概股共识", "广东上调最低工资"],
      "sentiment": "neutral",
      "sentiment_score": 0.2,
      "risk_warnings": ["投资有风险"],
      "data_citations": ["恒指涨1.75%"],
      "tags": ["中概股", "港股"]
    },
    "comment_analysis": {
      "total_count": 963,
      "fetched_count": 20,
      "sentiment_bullish": 0.6,
      "sentiment_bearish": 0.1,
      "sentiment_neutral": 0.3,
      "keywords": ["看好", "买入"],
      "hot_comments": [
        {"content": "支持李大霄", "likes": 120, "sentiment": "bullish"}
      ]
    },
    "danmaku_analysis": {
      "total_count": 500,
      "sampled_count": 100,
      "sentiment_bullish": 0.5,
      "sentiment_bearish": 0.2,
      "sentiment_neutral": 0.3,
      "keywords": ["反弹", "底部"]
    }
  }
}
```

---

## 5. 时间线

### GET /api/feed 🔒
视频+动态混合时间线。

参数:
| 参数 | 类型 | 说明 |
|------|------|------|
| limit | int | 条数，默认50 |
| blogger | int | 按博主筛选 |

```json
{
  "code": 0,
  "data": {
    "items": [
      {
        "type": "video",
        "bvid": "BV1VEba6HEvN",
        "title": "...",
        "sentiment": "neutral",
        "summary": "...",
        "publish_time": "2026-08-17T15:25:12"
      }
    ]
  }
}
```

---

## 6. 每日汇总

### GET /api/daily 🔒
可用日期列表。
```json
{"code": 0, "data": {"dates": ["2026-08-17", "2026-08-16"]}}
```

### GET /api/daily/{date} 🔒
某日汇总。
```json
{
  "code": 0,
  "data": {
    "date": "2026-08-17",
    "content": {
      "date": "2026-08-17",
      "blogger": "李大霄",
      "overall_sentiment": "neutral",
      "sentiment_score": 0.2,
      "key_points": ["德鲁肯米勒买入中概股", "港股反弹"],
      "risk_warnings": ["美债收益率飙涨"],
      "cross_analysis": "",
      "generated_at": "2026-08-17T21:30:00"
    }
  }
}
```

---

## 7. 操作接口 🔒

### POST /api/fetch/trigger
手动触发抓取。后台执行，立即返回。
```json
{"code": 0, "message": "抓取任务已触发"}
```

### POST /api/fetch/backfill
历史补抓。
```json
{"since": "2026-07-01", "mid": 2137589551}
```

---

## 情绪说明

| 值 | 含义 | score范围 |
|---|---|---|
| bullish | 看多 | 0.3 ~ 1.0 |
| bearish | 看空 | -1.0 ~ -0.3 |
| neutral | 中性 | -0.3 ~ 0.3 |

## 错误响应

```json
{"detail": "缺少X-API-Key header"}    // 401
{"detail": "无效的API Key"}           // 401
{"detail": "已有用户存在"}             // 409
{"detail": "Not Found"}               // 404
```
