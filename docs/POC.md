# POC测试记录

## 测试日期: 2026-08-17

## 测试目标
验证B站视频内容获取 → 语音转写 → AI摘要的完整管道

---

## 测试视频

| BV号 | 标题 | 博主 | 时长 |
|------|------|------|------|
| BV1cvqbBkED2 | 2026 防守反击 | 李大霄 | 5min |
| BV1memyB9ExY | 展望2026机会与挑战 | 李大霄 | 5.5min |
| BV1nbqmBbEf3 | 回望2025展望2026 | 李大霄 | 5.7min |
| BV1VEba6HEvN | 华尔街投资人再度买入中概股 | 李大霄 | 3.7min |
| BV11LbY6GEJX | 要与好人为伍 | 李大霄 | 5.1min |
| BV1fbbY6YExM | 美债收益率的飙涨还没引起重视 | 李大霄 | 6.1min |

---

## 测试结果

### 1. B站音频下载
- **方法**: `x/player/playurl?fnval=16` 获取dash音频流URL → curl下载
- **结果**: ✅ 全部成功，无需cookie，需Referer header
- **速度**: 秒级

### 2. MiMo ASR (mimo-v2.5-asr)
- **方法**: base64编码mp3 → chat completions API
- **结果**: 
  - ≤5.5分钟视频: ✅ 全部成功，质量优秀
  - >5.5分钟视频: ❌ 出现重复循环（8K上下文溢出）
- **解决方案**: 自动切片≤3分钟分别转写再拼接
- **速度**: 11-32秒/视频

### 3. faster-whisper (tiny/CPU)
- **结果**: ⚠️ 质量差，大量错字（"韩能"→"寒冷"，"47个一"→"47个亿"）
- **速度**: 48-70秒/视频
- **结论**: 淘汰，MiMo ASR全面碾压

### 4. B站AI字幕 (ai-zh)
- **方法**: `x/player/wbi/v2` + SESSDATA cookie → 获取带auth_key的字幕URL
- **结果**: ✅ 秒级获取，带精确时间戳，质量最好
- **覆盖率测试**: 19个视频中11个有字幕（58%）
- **结论**: 作为首选方案，ASR作为降级

### 5. 降级链验证
```
CC字幕 → ai-zh字幕 → MiMo ASR（切片≤3min）
```
- ✅ 覆盖率: 100%（三层兜底）
- ✅ 质量: ai-zh > MiMo ASR >> faster-whisper

---

## 开源项目分析

| 项目 | Stars | 类型 | 适用性 |
|------|-------|------|--------|
| bilibili-subtitle | 1171 | Chrome扩展 | 参考prompt设计 |
| bili-note | 260 | Python CLI | ⭐ 主要参考，复用WBI签名+提取逻辑 |
| bilili | 1179 | Python下载器 | 参考反412技巧，GPLv3注意 |

---

## 待解决问题

1. ~~ASR循环问题~~ → 已通过切片解决
2. ~~字幕获取~~ → cookie+WBI已验证
3. B站cookie过期处理 → 需要定期检查+提醒机制
4. ASR API key获取 → 当前key被系统脱敏，需单独配置
