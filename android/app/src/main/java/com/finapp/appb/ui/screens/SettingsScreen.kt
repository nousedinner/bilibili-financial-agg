package com.finapp.appb.ui.screens

import android.widget.Toast
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.finapp.appb.ui.theme.TextSecondary
import com.finapp.appb.viewmodel.SettingsViewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    viewModel: SettingsViewModel,
    onLogout: () -> Unit
) {
    val context = LocalContext.current
    val status by viewModel.status.collectAsState()
    val isTriggering by viewModel.isTriggering.collectAsState()
    val toast by viewModel.toast.collectAsState()
    val baseUrl by viewModel.baseUrl.collectAsState(initial = "")
    val apiKey by viewModel.apiKey.collectAsState(initial = "")

    LaunchedEffect(Unit) { viewModel.loadStatus() }
    LaunchedEffect(toast) {
        toast?.let {
            Toast.makeText(context, it, Toast.LENGTH_SHORT).show()
            viewModel.clearToast()
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(title = { Text("设置", fontWeight = FontWeight.Bold) },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.background))
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            // ── API Config ──
            Card(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(12.dp)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Text("连接配置", fontWeight = FontWeight.SemiBold, fontSize = 15.sp)
                    Spacer(modifier = Modifier.height(8.dp))
                    Text("API: $baseUrl", fontSize = 13.sp, color = TextSecondary)
                    Text("Key: ${apiKey.take(8)}...", fontSize = 13.sp, color = TextSecondary)
                }
            }

            // ── System Status ──
            status?.let { s ->
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(12.dp)
                ) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Text("系统状态", fontWeight = FontWeight.SemiBold, fontSize = 15.sp)
                        Spacer(modifier = Modifier.height(8.dp))
                        Row(horizontalArrangement = Arrangement.spacedBy(24.dp)) {
                            StatItem("博主", s.bloggers)
                            StatItem("视频", s.videos)
                            StatItem("失败", s.failed)
                        }
                        Spacer(modifier = Modifier.height(6.dp))
                        Text("Cookie: ${if (s.cookieValid) "✅ 有效" else "❌ 无效"}", fontSize = 13.sp, color = TextSecondary)
                        Text("上次抓取: ${s.lastFetch.take(16)}", fontSize = 13.sp, color = TextSecondary)
                    }
                }
            }

            // ── Actions ──
            Button(
                onClick = { viewModel.triggerFetch() },
                modifier = Modifier.fillMaxWidth().height(48.dp),
                shape = RoundedCornerShape(12.dp),
                enabled = !isTriggering
            ) {
                if (isTriggering) {
                    CircularProgressIndicator(modifier = Modifier.size(22.dp), strokeWidth = 2.dp, color = MaterialTheme.colorScheme.onPrimary)
                } else {
                    Text("手动触发抓取", fontSize = 15.sp)
                }
            }

            OutlinedButton(
                onClick = {
                    viewModel.logout()
                    onLogout()
                },
                modifier = Modifier.fillMaxWidth().height(48.dp),
                shape = RoundedCornerShape(12.dp),
                colors = ButtonDefaults.outlinedButtonColors(contentColor = MaterialTheme.colorScheme.error)
            ) {
                Text("退出登录", fontSize = 15.sp)
            }

            Spacer(modifier = Modifier.height(16.dp))
            Text(
                "财经聚合 v1.0.0",
                fontSize = 12.sp,
                color = TextSecondary,
                modifier = Modifier.align(Alignment.CenterHorizontally)
            )
        }
    }
}

@Composable
private fun StatItem(label: String, value: Int) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Text("$value", fontSize = 22.sp, fontWeight = FontWeight.Bold)
        Text(label, fontSize = 12.sp, color = TextSecondary)
    }
}
