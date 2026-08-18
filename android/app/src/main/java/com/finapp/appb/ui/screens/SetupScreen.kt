package com.finapp.appb.ui.screens

import android.widget.Toast
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.finapp.appb.FinApp
import com.finapp.appb.data.local.UserPreferences
import com.finapp.appb.data.repository.FinRepository
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SetupScreen(onSetupComplete: () -> Unit) {
    val context = LocalContext.current
    val app = context.applicationContext as FinApp
    val prefs = app.preferences
    val repo = app.repository
    val scope = rememberCoroutineScope()
    val focusManager = LocalFocusManager.current

    var apiUrl by remember { mutableStateOf("https://***REMOVED***") }
    var apiKey by remember { mutableStateOf("") }
    var username by remember { mutableStateOf("") }
    var showPassword by remember { mutableStateOf(false) }
    var isLoading by remember { mutableStateOf(false) }
    var isBootstrapping by remember { mutableStateOf(false) }
    var errorMsg by remember { mutableStateOf<String?>(null) }

    // Load saved config
    LaunchedEffect(Unit) {
        prefs.baseUrl.collect { if (it.isNotBlank()) apiUrl = it }
    }
    LaunchedEffect(Unit) {
        prefs.apiKey.collect { if (it.isNotBlank()) apiKey = it }
    }
    LaunchedEffect(Unit) {
        prefs.username.collect { if (it.isNotBlank()) username = it }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center
    ) {
        Text("💰 财经聚合", fontSize = 32.sp, fontWeight = FontWeight.Bold)
        Spacer(modifier = Modifier.height(8.dp))
        Text("首次使用请配置连接信息", fontSize = 14.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(modifier = Modifier.height(40.dp))

        // API URL
        OutlinedTextField(
            value = apiUrl,
            onValueChange = { apiUrl = it },
            label = { Text("API 地址") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
            shape = RoundedCornerShape(12.dp)
        )
        Spacer(modifier = Modifier.height(16.dp))

        // Username (for bootstrap)
        OutlinedTextField(
            value = username,
            onValueChange = { username = it },
            label = { Text("用户名") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
            shape = RoundedCornerShape(12.dp),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next)
        )
        Spacer(modifier = Modifier.height(16.dp))

        // API Key (password)
        OutlinedTextField(
            value = apiKey,
            onValueChange = { apiKey = it },
            label = { Text("密码 (API Key)") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
            shape = RoundedCornerShape(12.dp),
            visualTransformation = if (showPassword) VisualTransformation.None else PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(
                keyboardType = KeyboardType.Password,
                imeAction = ImeAction.Done
            ),
            keyboardActions = KeyboardActions(onDone = { focusManager.clearFocus() }),
            trailingIcon = {
                IconButton(onClick = { showPassword = !showPassword }) {
                    Icon(
                        if (showPassword) Icons.Default.VisibilityOff else Icons.Default.Visibility,
                        contentDescription = "切换密码可见性"
                    )
                }
            }
        )

        if (errorMsg != null) {
            Spacer(modifier = Modifier.height(8.dp))
            Text(errorMsg!!, color = MaterialTheme.colorScheme.error, fontSize = 13.sp)
        }

        Spacer(modifier = Modifier.height(24.dp))

        // Save & Connect
        Button(
            onClick = {
                focusManager.clearFocus()
                isLoading = true
                errorMsg = null
                scope.launch {
                    val ok = repo.healthCheck(apiUrl)
                    if (!ok) {
                        errorMsg = "无法连接到服务器"
                        isLoading = false
                        return@launch
                    }
                    if (apiKey.isBlank()) {
                        errorMsg = "请输入密码"
                        isLoading = false
                        return@launch
                    }
                    prefs.saveConfig(apiUrl, apiKey, username)
                    isLoading = false
                    onSetupComplete()
                }
            },
            modifier = Modifier.fillMaxWidth().height(52.dp),
            shape = RoundedCornerShape(12.dp),
            enabled = !isLoading && !isBootstrapping
        ) {
            if (isLoading) {
                CircularProgressIndicator(modifier = Modifier.size(24.dp), color = MaterialTheme.colorScheme.onPrimary, strokeWidth = 2.dp)
            } else {
                Text("保存并连接", fontSize = 16.sp)
            }
        }

        Spacer(modifier = Modifier.height(12.dp))

        // Bootstrap button
        OutlinedButton(
            onClick = {
                focusManager.clearFocus()
                isBootstrapping = true
                errorMsg = null
                scope.launch {
                    val uname = username.ifBlank { "android" }
                    val result = repo.bootstrap(apiUrl, uname)
                    result.onSuccess { user ->
                        apiKey = user.apiKey
                        username = user.username
                        errorMsg = null
                        Toast.makeText(context, "用户创建成功！密码已自动填入", Toast.LENGTH_LONG).show()
                    }.onFailure {
                        errorMsg = "创建用户失败: ${it.message}"
                    }
                    isBootstrapping = false
                }
            },
            modifier = Modifier.fillMaxWidth().height(48.dp),
            shape = RoundedCornerShape(12.dp),
            enabled = !isLoading && !isBootstrapping
        ) {
            if (isBootstrapping) {
                CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp)
            } else {
                Text("没有账号？创建用户", fontSize = 14.sp)
            }
        }
    }
}
