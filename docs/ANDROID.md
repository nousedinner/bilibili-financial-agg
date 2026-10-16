# Android 构建与使用

## 构建

需要 JDK 17、Android SDK Platform 35、Build Tools 34.0.0。APP 支持 Android 8.0 及以上。Gradle Wrapper 随源码提供，无需手动安装指定版本的 Gradle。

配置 `ANDROID_HOME` 或不入 Git 的 `android/local.properties` 中的 `sdk.dir`。Windows 请在纯英文路径编译，避免 Android 工具链的中文路径问题。

```bash
cd android
./gradlew :app:assembleDebug
```

Windows 使用 `gradlew.bat :app:assembleDebug`。产物为 `app/build/outputs/apk/debug/app-debug.apk`。正式应用包名为 `com.finapp.appb`，当前版本 1.1.3 / versionCode 5。

自用可以安装 Debug 包；覆盖安装必须使用原来的签名证书。更换构建机器或签名可能导致无法覆盖已有安装。分发 Release 时执行 `:app:assembleRelease` 并自行配置或完成签名，签名证书和密码不要提交到仓库。

默认通知方式无需 Firebase 账号。若需要启用可选 FCM，可将 `app/firebase.properties.example` 复制为 `app/firebase.properties` 并填写自己的公共客户端配置；服务账号私钥只属于服务器。

## 连接与使用

1. 首次打开“财经聚合”，输入可访问的 HTTPS 服务器地址和部署时生成的连接 KEY。
2. 连接成功后添加需要关注的博主，等待定时采集；也可在设置中手动触发一次抓取。
3. 在信息流查看摘要，在日报中查看当天汇总。服务器尚未采集的历史视频不会自动全部补抓。

连接 KEY 是访问自己后端的凭据，由服务器首次部署时生成，APP 直接使用已有的这一把。它和 B 站登录授权分别管理，无需额外填写管理密钥。

## 重新授权服务器

设置页中的 B 站授权状态表示服务器保存的会话，与手机官方 B 站 APP 的登录状态相互独立。

1. 在“服务器 B 站授权”页面生成二维码并保存到相册。
2. 打开官方哔哩哔哩的“扫一扫 → 相册”，选择刚保存的二维码，并确认登录。
3. 返回财经聚合等待状态更新。二维码约 3 分钟失效，过期后重新生成。

服务器只接收自己创建会话的授权结果，校验账号后更新凭据。整个流程使用普通应用权限，不读取官方 APP 私有数据，不要求 ROOT。验证 APP 只是早期测试用途，正式 APP 已包含此功能。

## 授权通知

在授权页面启用通知，并按系统提示授予通知权限。默认在 APP 启动时检查服务器事件，并由系统后台任务约每 15 分钟检查；可能因系统省电、网络或应用被强行停止而延迟。该方式无需 FCM 配置，也不承诺实时送达。

通知中的“授权需要处理”可点击进入重新授权页面；授权恢复后关闭失效提醒并记录恢复。412、网络超时或风控不会单独触发登录过期告警。通知不会展示 Cookie 或连接 KEY。

连接 KEY 不进入 Android 云备份或设备迁移备份。原地升级保留已有连接；卸载或清除数据后需重新填写，操作前请确认自己保存了 KEY。
