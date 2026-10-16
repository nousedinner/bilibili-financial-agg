package com.finapp.appb.ui.screens

import android.Manifest
import android.content.ContentValues
import android.os.Build
import android.provider.MediaStore
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.finapp.appb.notifications.AuthorizationNotifications
import com.finapp.appb.viewmodel.AuthorizationViewModel
import com.finapp.appb.BuildConfig

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AuthorizationScreen(viewModel: AuthorizationViewModel, onBack: () -> Unit) {
    val state by viewModel.state.collectAsState()
    val context = LocalContext.current
    var key by remember { mutableStateOf("") }
    var savedMessage by remember { mutableStateOf<String?>(null) }
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { viewModel.enableNotifications() }
    val document = rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("image/png")) { uri ->
        uri?.let { target -> state.qr?.let { bitmap -> context.contentResolver.openOutputStream(target)?.use { bitmap.compress(android.graphics.Bitmap.CompressFormat.PNG,100,it) } } }
    }
    Scaffold(topBar = { TopAppBar(title = { Text("服务器 B 站授权") }, navigationIcon = { TextButton(onClick=onBack) { Text("返回") } }) }) { padding ->
        Column(Modifier.fillMaxSize().padding(padding).verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement=Arrangement.spacedBy(12.dp)) {
            Text(when(state.status?.state) { "valid" -> "授权有效"; "invalid" -> "需要重新授权"; else -> "暂时无法确认授权状态" })
            if (state.status?.state == "unknown" && state.status?.lastConfirmed == "invalid") Text("上次已确认失效，尚未确认恢复")
            Text("这里检查的是服务器的 B 站会话，与手机上哔哩哔哩的登录状态分开。")
            Button(onClick = {
                if (Build.VERSION.SDK_INT >= 33 && !AuthorizationNotifications.permitted(context)) permission.launch(Manifest.permission.POST_NOTIFICATIONS)
                else viewModel.enableNotifications()
            }, modifier=Modifier.fillMaxWidth()) { Text("启用授权告警") }
            Text(if (state.status?.push?.verified == true) "已确认收到过服务器测试推送" else "即时推送尚未完成送达确认")
            if (AuthorizationNotifications.enabled(context)) Text("后台定期检查已启用，可能受系统省电影响而延迟。")
            OutlinedButton(onClick={viewModel.testNotification()},modifier=Modifier.fillMaxWidth()) { Text("发送服务器测试通知") }
            HorizontalDivider()
            OutlinedTextField(value=key,onValueChange={key=it},label={Text(if(state.keySaved) "替换管理密钥（可留空）" else "服务器独立管理密钥")},
                singleLine=true,visualTransformation=PasswordVisualTransformation(),keyboardOptions=KeyboardOptions(keyboardType=KeyboardType.Password),modifier=Modifier.fillMaxWidth())
            TextButton(onClick={viewModel.saveKey(key);key=""},enabled=key.isNotBlank()) { Text("保存管理密钥") }
            Button(onClick={viewModel.startLogin()},enabled=!state.busy && BuildConfig.AUTHORIZATION_POC,modifier=Modifier.fillMaxWidth()) { Text("重新授权服务器") }
            if(!BuildConfig.AUTHORIZATION_POC) Text("同机授权入口待实际手机验证后启用，当前不会替换服务器凭据。")
            if(state.busy) LinearProgressIndicator(modifier=Modifier.fillMaxWidth())
            state.qr?.let { bitmap ->
                Image(bitmap.asImageBitmap(),contentDescription="服务器授权二维码",modifier=Modifier.fillMaxWidth().height(260.dp))
                OutlinedButton(onClick={
                    if(Build.VERSION.SDK_INT >= 29) {
                        try {
                            val values=ContentValues().apply {
                                put(MediaStore.Images.Media.DISPLAY_NAME,"财经聚合授权-${System.currentTimeMillis()}.png")
                                put(MediaStore.Images.Media.MIME_TYPE,"image/png")
                                put(MediaStore.Images.Media.RELATIVE_PATH,"Pictures/财经聚合")
                                put(MediaStore.Images.Media.IS_PENDING,1)
                            }
                            val uri=context.contentResolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI,values) ?: error("无法创建图片")
                            try {
                                context.contentResolver.openOutputStream(uri)?.use { bitmap.compress(android.graphics.Bitmap.CompressFormat.PNG,100,it) } ?: error("无法保存图片")
                                context.contentResolver.update(uri,ContentValues().apply { put(MediaStore.Images.Media.IS_PENDING,0) },null,null)
                            } catch(error:Exception) { context.contentResolver.delete(uri,null,null);throw error }
                            savedMessage="二维码已保存，请在哔哩哔哩扫一扫中从相册识别并确认"
                        } catch(error:Exception) { savedMessage=error.message ?: "保存失败" }
                    } else document.launch("财经聚合授权.png")
                },modifier=Modifier.fillMaxWidth()) { Text("保存二维码到相册") }
                Text("二维码有效期约三分钟。手机上的确认流程仍需实际验证，完成前不会替换服务器凭据。")
            }
            state.message?.let { Text(it) }
            savedMessage?.let { Text(it) }
            TextButton(onClick={viewModel.refresh()}) { Text("刷新状态") }
        }
    }
}
