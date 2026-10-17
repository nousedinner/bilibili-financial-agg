# 后端回归验证

完整 132 用例均已纳入仓库：历史验收、生命周期、回归测试共 115 个，Issue 21 独立回归 13 个，长视频恢复回归 4 个。无需私有验证快照、生产配置或生产密钥。

推荐从干净检出构建独立测试镜像，再断网执行；镜像包含 FFmpeg/FFprobe，媒体用例不会因缺少工具跳过：

```sh
docker build -f tests/Dockerfile -t fin-agg-tests .
docker run --rm --network none fin-agg-tests
```

测试构建使用 `tests/Dockerfile.dockerignore` 白名单，只包含测试和运行源码等必要输入；生产 Dockerfile 及其白名单未修改，测试文件不进入生产镜像。

也可使用 Python 3.12 虚拟环境，安装 FFmpeg/FFprobe 并将其加入 PATH 后执行：

```sh
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
```

测试使用临时 SQLite、模拟上游和测试密钥。单独运行本次回归可用 `python -m unittest tests.test_issue21 -v`。

长视频恢复回归可用 `python -m unittest tests.test_long_analysis -v`：覆盖分段耗尽后通过 HTTP 接口显式恢复、成功分段复用及最终发布，最终合并自身的三次上限，分段与合并共享每日预算，以及中间结果未缩短时仍有限退出。

历史套件有三处夹具已修正：两处日报调用适配当前 `_generate_digest(bloggers, target_date)` 签名；二维码双域名用例推进模拟时钟，遵守创建会话的 10 秒限频。原始断言保留，无删除或跳过用例；生产逻辑及限频不变。

`mysql_issue21_check.py` 仅用于专用隔离 MySQL 容器中的生产备份副本，要求数据库名以 `fin_agg_audit_` 开头、数据库地址为 `127.0.0.1`、模型密钥为 `TEST_ONLY`、SESSDATA 为空。不要将它连到共享生产 MySQL。副本应保留 schema 8 和混合排序规则；脚本验证迁移、完整数据指纹、字段定义、幂等性、列规则漂移、新安装和唯一键冲突。脚本会创建 `fin_agg_audit_fresh`，应在一次性隔离实例运行。

`mysql_isolation_check.py` 是历史所有权及检查点集成验证，同样要求显式选择 `fin_agg_audit_` 数据库和测试密钥，不在默认离线套件中执行。MySQL 集成脚本必须使用专用隔离实例。
