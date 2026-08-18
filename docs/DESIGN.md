# 设计文档 v6 — 最终版

## 1. 项目目标

每晚21:30自动抓取B站财经博主的视频、动态、评论、弹幕，生成两层分析（单视频分析 + 当日汇总），通过REST API提供给安卓APP消费。

**输出方式：** 纯API，不做推送。安卓APP直接调接口获取数据。

---

## 2. 确认决策

| 项目 | 决策 |
|------|------|
| 语言+框架 | Python + FastAPI |
| 数据库 | MySQL（加入现有mysql-net网络，新建专属用户） |
| 部署 | Docker，home.cancanneed.top/fin-api/ |
| AI模型 | MiMo（配置文件可换） |
| ASR | MiMo ASR，切片≤3min |
| 初始博主 | 李大霄 (mid: 2137589551) |
| 抓取频率 | 每晚21:30 |
| 输出方式 | 纯REST API（安卓APP消费） |
| 弹幕 | 采样≤2000条 |
| LLM | 评论+弹幕合并一次调用 |
| 历史补抓 | 从2026-07-01，每次≤20条 |

### 约束
- 磁盘2.6GB，不缓存音频
- 内存4GB，服务<200MB
- SESSDATA约2026-09-04过期
- 单视频间隔3-5秒，指数退避

---

## 3. 数据存储 — MySQL

### 连接
- 加入现有Docker网络 `mysql-net`
- 容器内用主机名 `mysql:3306` 访问
- 专属用户 `fin_agg`，只操作 `fin_agg` 数据库

### 建库+建用户
```sql
CREATE DATABASE IF NOT EXISTS fin_agg
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER 'fin_agg'@'%' IDENTIFIED BY '随机密码';
GRANT SELECT, INSERT, UPDATE, DELETE ON fin_agg.* TO 'fin_agg'@'%';
FLUSH PRIVILEGES;
```

### 表结构

```sql
CREATE TABLE bloggers (
    mid BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    tags JSON,
    enabled BOOLEAN DEFAULT TRUE,
    added_at DATETIME DEFAULT CURRENT_TIMESTAMP
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE videos (
    bvid VARCHAR(20) PRIMARY KEY,
    mid BIGINT NOT NULL,
    title VARCHAR(500),
    duration INT,
    publish_time DATETIME,
    view_count INT DEFAULT 0,
    content_type ENUM('video','dynamic') DEFAULT 'video',
    dyn_id VARCHAR(50),
    fetch_status ENUM('pending','ok','failed') DEFAULT 'pending',
    error_message TEXT,
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_mid (mid),
    INDEX idx_publish (publish_time),
    INDEX idx_status (fetch_status)
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE transcripts (
    bvid VARCHAR(20) PRIMARY KEY,
    source ENUM('cc_subtitle','ai_subtitle','asr') NOT NULL,
    full_text LONGTEXT,
    segment_count INT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE summaries (
    bvid VARCHAR(20) PRIMARY KEY,
    summary TEXT,
    key_points JSON,
    sentiment ENUM('bullish','bearish','neutral'),
    sentiment_score FLOAT,
    risk_warnings JSON,
    data_citations JSON,
    tags JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE comment_analysis (
    ref_id VARCHAR(50) NOT NULL,
    ref_type ENUM('video','dynamic') DEFAULT 'video',
    total_count INT,
    fetched_count INT,
    sentiment_bullish FLOAT,
    sentiment_bearish FLOAT,
    sentiment_neutral FLOAT,
    hot_comments JSON,
    keywords JSON,
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ref_id, ref_type)
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE danmaku_analysis (
    bvid VARCHAR(20) PRIMARY KEY,
    total_count INT,
    sampled_count INT,
    sentiment_bullish FLOAT,
    sentiment_bearish FLOAT,
    sentiment_neutral FLOAT,
    keywords JSON,
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE dynamics (
    dyn_id VARCHAR(50) PRIMARY KEY,
    mid BIGINT NOT NULL,
    content TEXT,
    publish_time DATETIME,
    summary TEXT,
    sentiment ENUM('bullish','bearish','neutral'),
    tags JSON,
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_mid (mid),
    INDEX idx_publish (publish_time)
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE fetch_watermark (
    mid BIGINT PRIMARY KEY,
    last_bvid VARCHAR(20),
    last_publish_time DATETIME,
    last_dyn_id VARCHAR(50),
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE daily_digests (
    digest_date DATE PRIMARY KEY,
    content JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) DEFAULT CHARSET=utf8mb4;
```

---

## 4. 数据获取流程

### 4.1 每晚21:30

```
21:30 触发 → 验证SESSDATA → 无效则记录，终止
  ↓
遍历blogger → 拉最新动态(WBI) → 对比watermark → 只处理新内容
  ↓ 首次运行：限制最近20条
每条新内容：
  视频：字幕 → 评论+弹幕 → LLM合并分析
  图文：读文本 → 评论分析
  ↓ 单条失败 → fetch_status='failed'
更新watermark → 生成当日汇总存DB
```

### 4.2 单视频管线

```
Step 1: 字幕（CC → ai-zh → ASR切片≤3min）
Step 2: 评论top20 + 弹幕采样≤2000
Step 3: LLM一次调用 → 摘要+观点+情绪+评论情绪+弹幕情绪
```

### 4.3 反限速
- 标准UA + Referer
- 间隔3-5秒
- -412/-352 → 指数退避(10/20/40s)
- WBI key缓存+定期刷新

---

## 5. Cookie管理

- SESSDATA存环境变量
- 从cookie时间戳解析过期
- 每日任务前验证
- 过期记录到DB，API暴露状态

---

## 6. API鉴权

- GET：公开（APP读取）
- POST/DELETE：X-API-Key header
- Key存环境变量 FIN_AGG_API_KEY

---

## 7. Docker部署

### docker-compose.yml
```yaml
services:
  fin-agg:
    build: .
    ports:
      - "127.0.0.1:8091:8091"
    networks:
      - mysql-net
    volumes:
      - ./config:/app/config:ro
    env_file:
      - .env
    environment:
      - TZ=Asia/Shanghai
      - DB_HOST=mysql
      - DB_PORT=3306
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/api/health')"]
      interval: 60s
      timeout: 5s
      retries: 3

networks:
  mysql-net:
    external: true
```

### Dockerfile
```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8091"]
```

### Nginx
```nginx
location = /fin-api { return 301 /fin-api/; }
location /fin-api/ {
    proxy_pass http://127.0.0.1:8091/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

---

## 8. 降级与容错

| 场景 | 处理 |
|------|------|
| CC字幕 | 直接用 |
| ai-zh字幕 | cookie+WBI |
| 无字幕 | ASR切片≤3min |
| 单条失败 | fetch_status='failed' + error |
| 失败重试 | 下次运行自动重试 |
| 弹幕>5000 | 随机采样2000 |
| Cookie无效 | 记录状态，API可查 |
| API限速 | 指数退避 |
| 首次运行 | 限制20条 |
