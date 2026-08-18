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
| FIN_AGG_API_KEY | API鉴权密钥 |

## 快速开始

```bash
# TODO
```
