package com.finapp.appb.ui.screens

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
import com.finapp.appb.ui.friendlyErrorMessage
import kotlinx.coroutines.launch
import kotlinx.coroutines.flow.first

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SetupScreen(onSetupComplete: () -> Unit) {
    val context = LocalContext.current
    val app = context.applicationContext as FinApp
    val prefs = app.preferences
    val repo = app.repository
    val scope = rememberCoroutineScope()
    val focusManager = LocalFocusManager.current

    var apiUrl by remember { mutableStateOf("https://home.cancanneed.top/fin-api/") }
    var apiKey by remember { mutableStateOf("") }
    var username by remember { mutableStateOf("") }
    var showPassword by remember { mutableStateOf(false) }
    var isLoading by remember { mutableStateOf(false) }
    var errorMsg by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) {
        val config = prefs.config.first()
        if (config.url.isNotBlank()) apiUrl = config.url
        apiKey = config.key
        username = config.username
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
            enabled = !isLoading,
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
            enabled = !isLoading,
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
            enabled = !isLoading,
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
                val urlSnapshot = apiUrl
                val keySnapshot = apiKey
                val userSnapshot = username
                scope.launch {
                    try {
                        repo.connect(urlSnapshot, keySnapshot, userSnapshot)
                            .onSuccess { onSetupComplete() }
                            .onFailure { errorMsg = friendlyErrorMessage(it.message) ?: "连接失败，请检查地址和 API Key" }
                    } finally { isLoading = false }
                }
            },
            modifier = Modifier.fillMaxWidth().height(52.dp),
            shape = RoundedCornerShape(12.dp),
            enabled = !isLoading
        ) {
            if (isLoading) {
                CircularProgressIndicator(modifier = Modifier.size(24.dp), color = MaterialTheme.colorScheme.onPrimary, strokeWidth = 2.dp)
            } else {
                Text("保存并连接", fontSize = 16.sp)
            }
        }
    }
}
