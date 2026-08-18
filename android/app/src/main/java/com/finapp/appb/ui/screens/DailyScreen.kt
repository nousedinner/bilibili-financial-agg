package com.finapp.appb.ui.screens

import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.finapp.appb.data.api.DailyContent
import com.finapp.appb.ui.friendlyErrorMessage
import com.finapp.appb.ui.theme.*
import com.finapp.appb.viewmodel.DailyViewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DailyScreen(viewModel: DailyViewModel) {
    val summaries by viewModel.summaries.collectAsState()
    val isLoading by viewModel.isLoading.collectAsState()
    val error by viewModel.error.collectAsState()
    val expandedDate by viewModel.expandedDate.collectAsState()

    // 返回键拦截：有展开的卡片时先收起，否则放行
    BackHandler(enabled = expandedDate != null) {
        viewModel.toggleExpand(expandedDate ?: "")
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("每日汇总", fontWeight = FontWeight.Bold) },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.background)
            )
        }
    ) { padding ->
        when {
            isLoading && summaries.isEmpty() -> Box(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentAlignment = Alignment.Center
            ) { CircularProgressIndicator() }

            error != null && summaries.isEmpty() -> Box(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentAlignment = Alignment.Center
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text(friendlyErrorMessage(error), color = MaterialTheme.colorScheme.error)
                    Spacer(modifier = Modifier.height(12.dp))
                    Button(onClick = { viewModel.loadAll() }) { Text("重试") }
                }
            }

            summaries.isEmpty() -> Box(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentAlignment = Alignment.Center
            ) {
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Text("📭", fontSize = 48.sp)
                    Text("暂无汇总数据", fontSize = 16.sp, color = TextSecondary)
                    Text(
                        "每日汇总由后端自动生成\n请稍后再来查看",
                        fontSize = 13.sp,
                        color = TextSecondary
                    )
                }
            }

            else -> LazyColumn(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentPadding = PaddingValues(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                items(summaries, key = { it.date ?: "" }) { item ->
                    DailyCard(
                        item = item,
                        isExpanded = expandedDate == item.date,
                        onClick = { viewModel.toggleExpand(item.date ?: "") }
                    )
                }
            }
        }
    }
}

@Composable
private fun DailyCard(
    item: DailyContent,
    isExpanded: Boolean,
    onClick: () -> Unit
) {
    val sentiment = com.finapp.appb.ui.components.Sentiment.fromString(item.overallSentiment ?: "neutral")
    val score = item.sentimentScore ?: 0.0
    val bloggers = item.bloggers?.joinToString("、") ?: ""
    val summary = item.summary ?: ""

    Card(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            // 头部：日期 + 情绪标签
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = item.date ?: "",
                    fontSize = 18.sp,
                    fontWeight = FontWeight.Bold
                )
                Surface(
                    shape = RoundedCornerShape(6.dp),
                    color = sentiment.color.copy(alpha = 0.15f)
                ) {
                    Text(
                        text = "${sentiment.icon} ${sentiment.label}",
                        modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp),
                        fontSize = 12.sp,
                        color = sentiment.color,
                        fontWeight = FontWeight.SemiBold
                    )
                }
            }

            // 博主（API 数据不完整，暂不显示）
            // if (bloggers.isNotEmpty()) {
            //     Text(
            //         text = "👤 $bloggers",
            //         fontSize = 13.sp,
            //         color = TextSecondary,
            //         modifier = Modifier.padding(top = 4.dp)
            //     )
            // }

            // 分数
            Text(
                text = "情绪分数: ${String.format("%.2f", score)}",
                fontSize = 12.sp,
                color = TextSecondary,
                modifier = Modifier.padding(top = 2.dp)
            )

            // 摘要（截断）
            if (summary.isNotEmpty()) {
                Text(
                    text = summary,
                    fontSize = 14.sp,
                    color = TextPrimary,
                    maxLines = if (isExpanded) Int.MAX_VALUE else 3,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.padding(top = 8.dp),
                    lineHeight = 20.sp
                )
            }

            // 展开详情
            AnimatedVisibility(visible = isExpanded) {
                Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    // 共识
                    val consensus = item.consensus ?: emptyList()
                    if (consensus.isNotEmpty()) {
                        DetailSection(title = "🤝 共识观点") {
                            consensus.forEach { Text("• $it", fontSize = 14.sp, lineHeight = 20.sp) }
                        }
                    }

                    // 关键话题
                    val keyTopics = item.keyTopics ?: emptyList()
                    if (keyTopics.isNotEmpty()) {
                        DetailSection(title = "📌 关键话题") {
                            keyTopics.forEach { Text("• $it", fontSize = 14.sp, lineHeight = 20.sp) }
                        }
                    }

                    // 分歧点
                    val differences = item.differences ?: emptyList()
                    if (differences.isNotEmpty()) {
                        DetailSection(title = "⚡ 分歧观点", titleColor = WarningRed) {
                            differences.forEach {
                                Text("• $it", fontSize = 14.sp, color = WarningRed, lineHeight = 20.sp)
                            }
                        }
                    }
                }
            }

            // 展开/收起提示
            Text(
                text = if (isExpanded) "收起 ▲" else "展开详情 ▼",
                fontSize = 12.sp,
                color = Primary,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(top = 8.dp),
            )
        }
    }
}

@Composable
private fun DetailSection(
    title: String,
    titleColor: androidx.compose.ui.graphics.Color = TextPrimary,
    content: @Composable ColumnScope.() -> Unit
) {
    Column(modifier = Modifier.padding(top = 4.dp)) {
        Text(title, fontWeight = FontWeight.SemiBold, fontSize = 14.sp, color = titleColor)
        Spacer(modifier = Modifier.height(6.dp))
        content()
    }
}
