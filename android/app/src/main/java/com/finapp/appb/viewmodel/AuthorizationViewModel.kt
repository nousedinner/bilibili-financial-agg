package com.finapp.appb.viewmodel

import android.app.Application
import android.graphics.Bitmap
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.finapp.appb.FinApp
import com.finapp.appb.data.api.AuthorizationStatus
import com.finapp.appb.data.api.LoginStart
import com.finapp.appb.data.local.ManagementKeyStore
import com.finapp.appb.data.repository.waitForBiliLogin
import com.finapp.appb.notifications.AuthorizationNotifications
import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

data class AuthorizationUiState(val status: AuthorizationStatus? = null, val busy: Boolean = false,
    val message: String? = null, val qr: Bitmap? = null, val keySaved: Boolean = false)

class AuthorizationViewModel(application: Application) : AndroidViewModel(application) {
    private val app = application as FinApp
    private val keys = ManagementKeyStore(application)
    private val mutable = MutableStateFlow(AuthorizationUiState())
    val state = mutable.asStateFlow()
    private var poll: Job? = null
    init { refresh() }
    fun refresh() = viewModelScope.launch {
        val server=app.preferences.config.first().url.trim().trimEnd('/') + "/"
        mutable.value=mutable.value.copy(keySaved=keys.read(server)!=null)
        app.repository.authorizationStatus().onSuccess { mutable.value = mutable.value.copy(status = it) }
            .onFailure { mutable.value = mutable.value.copy(message = it.message ?: "暂时无法读取授权状态") }
    }
    fun saveKey(value: String) = viewModelScope.launch {
        try { keys.save(value.trim(),app.preferences.config.first().url.trim().trimEnd('/') + "/"); mutable.value = mutable.value.copy(keySaved = true, message = "管理密钥已保存在系统密钥库保护的存储中") }
        catch (error: Exception) { mutable.value = mutable.value.copy(message = error.message ?: "管理密钥保存失败") }
    }
    fun enableNotifications() {
        AuthorizationNotifications.enable(app)
        mutable.value = mutable.value.copy(message = if (AuthorizationNotifications.permitted(app)) "已启用授权告警；可发送服务器测试通知" else "通知尚未获得允许，可在系统设置中开启")
        refresh()
    }
    fun testNotification() = viewModelScope.launch {
        if (!AuthorizationNotifications.pushConfigured()) {
            mutable.value = mutable.value.copy(message = "此安装包尚未配置即时推送；后台定期检查不能代替推送送达测试")
            return@launch
        }
        val status=app.repository.authorizationStatus().getOrNull()
        if(status?.push?.configured != true) {
            mutable.value=mutable.value.copy(message="服务器即时通知通道尚未配置或暂时无法确认")
            return@launch
        }
        AuthorizationNotifications.synchronize(app)
        app.repository.testPush().onSuccess { mutable.value = mutable.value.copy(message = "测试已提交，请将 APP 退到后台，收到后点击通知确认") }
            .onFailure { mutable.value = mutable.value.copy(message = it.message ?: "服务器未能发送测试通知") }
    }
    fun startLogin() {
        poll?.cancel()
        poll = viewModelScope.launch {
            mutable.value = mutable.value.copy(busy = true, qr = null, message = "正在生成一次新的服务器授权")
            try {
                val server=app.preferences.config.first().url.trim().trimEnd('/') + "/"
                val key=keys.read(server) ?: error("请先输入当前服务器的独立管理密钥")
                val login = app.repository.startBiliLogin(key,server).getOrThrow()
                val matrix = QRCodeWriter().encode(login.qrUrl, BarcodeFormat.QR_CODE, 600, 600)
                val bitmap = Bitmap.createBitmap(600, 600, Bitmap.Config.ARGB_8888)
                for (y in 0 until 600) for (x in 0 until 600) bitmap.setPixel(x, y, if (matrix[x,y]) 0xff000000.toInt() else 0xffffffff.toInt())
                mutable.value = mutable.value.copy(qr = bitmap, message = "保存二维码，用哔哩哔哩扫一扫从相册识别并确认，然后返回此页")
                val complete = waitForBiliLogin(login.expiresIn, poll = {
                        val result = app.repository.pollBiliLogin(login.id, login.nonce, key,server).getOrThrow()
                        result.state == "complete"
                    }, onTemporaryFailure = {
                        mutable.value = mutable.value.copy(message = "授权验证暂时无法完成，二维码有效期内会自动重试；请保留此会话")
                    })
                mutable.value = mutable.value.copy(qr = null, message = if (complete)
                    "服务器授权已验证并更新；历史视频不会因此批量重跑" else "二维码已到期，可重新发起授权")
                refresh()
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { mutable.value = mutable.value.copy(qr = null, message = error.message ?: "授权暂未完成，原有凭据保持不变") }
            finally { mutable.value = mutable.value.copy(busy = false) }
        }
    }
}
