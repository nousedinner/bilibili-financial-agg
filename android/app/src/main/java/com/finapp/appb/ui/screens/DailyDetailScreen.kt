package com.finapp.appb.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.finapp.appb.data.api.DailyContent
import com.finapp.appb.ui.FormatUtils
import com.finapp.appb.ui.components.SkeletonDetail
import com.finapp.appb.ui.friendlyErrorMessage
import com.finapp.appb.ui.theme.*
import com.finapp.appb.viewmodel.DailyDetailViewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DailyDetailScreen(
    date: String,
    viewModel: DailyDetailViewModel,
    onBack: () -> Unit
) {
    val detail by viewModel.detail.collectAsState()
    val isLoading by viewModel.isLoading.collectAsState()
    val error by viewModel.error.collectAsState()

    LaunchedEffect(date) { viewModel.loadDetail(date) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(date, fontSize = 16.sp) },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.background),
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, "返回")
                    }
                },
                actions = {
                    IconButton(onClick = { viewModel.loadDetail(date) }) {
                        Icon(Icons.Default.Refresh, "刷新")
                    }
                }
            )
        }
    ) { padding ->
        when {
            isLoading -> SkeletonDetail(modifier = Modifier.padding(padding))
            error != null -> Box(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentAlignment = Alignment.Center
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text(friendlyErrorMessage(error), color = MaterialTheme.colorScheme.error)
                    Spacer(modifier = Modifier.height(12.dp))
                    Button(onClick = { viewModel.loadDetail(date) }) { Text("重试") }
                }
            }
            detail != null -> {
                val d = detail!!
                Column(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(padding)
                        .verticalScroll(rememberScrollState())
                        .padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(16.dp)
                ) {
                    // 情绪标签 + 分数
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        val sentiment = com.finapp.appb.ui.components.Sentiment.fromString(d.overallSentiment)
                        Surface(
                            shape = RoundedCornerShape(6.dp),
                            color = sentiment.color.copy(alpha = 0.15f)
                        ) {
                            Text(
                                text = "${sentiment.icon} ${sentiment.label}",
                                modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp),
                                fontSize = 14.sp,
                                color = sentiment.color,
                                fontWeight = FontWeight.SemiBold
                            )
                        }
                        if (d.sentimentScore != null) {
                            Text(
                                text = "情绪分数: ${String.format("%.2f", d.sentimentScore)}",
                                fontSize = 14.sp,
                                color = TextSecondary
                            )
                        }
                    }

                    // 摘要
                    val summary = d.summary ?: ""
                    if (summary.isNotEmpty()) {
                        SectionCard(title = "📝 摘要") {
                            Text(summary, fontSize = 14.sp, lineHeight = 22.sp)
                        }
                    }

                    // 博主
                    val bloggers = d.bloggers ?: emptyList()
                    if (bloggers.isNotEmpty()) {
                        SectionCard(title = "👤 参与博主") {
                            Text(bloggers.joinToString("、"), fontSize = 14.sp, lineHeight = 20.sp)
                        }
                    }

                    // 共识观点
                    val consensus = d.consensus ?: emptyList()
                    if (consensus.isNotEmpty()) {
                        SectionCard(title = "🤝 共识观点") {
                            consensus.forEach { Text("• $it", fontSize = 14.sp, lineHeight = 20.sp) }
                        }
                    }

                    // 关键话题
                    val keyTopics = d.keyTopics ?: emptyList()
                    if (keyTopics.isNotEmpty()) {
                        SectionCard(title = "📌 关键话题") {
                            keyTopics.forEach { Text("• $it", fontSize = 14.sp, lineHeight = 20.sp) }
                        }
                    }

                    // 分歧观点
                    val differences = d.differences ?: emptyList()
                    if (differences.isNotEmpty()) {
                        SectionCard(title = "⚡ 分歧观点", titleColor = WarningRed) {
                            differences.forEach {
                                Text("• $it", fontSize = 14.sp, color = WarningRed, lineHeight = 20.sp)
                            }
                        }
                    }

                    Spacer(modifier = Modifier.height(32.dp))
                }
            }
        }
    }
}

@Composable
private fun SectionCard(
    title: String,
    titleColor: androidx.compose.ui.graphics.Color = TextPrimary,
    content: @Composable ColumnScope.() -> Unit
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(containerColor = CardBackground)
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(title, fontSize = 16.sp, fontWeight = FontWeight.Bold)
            Spacer(modifier = Modifier.height(10.dp))
            content()
        }
    }
}
