package com.finapp.appb.ui.screens

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
fun DailyScreen(
    viewModel: DailyViewModel,
    onDateClick: (String) -> Unit
) {
    val summaries by viewModel.summaries.collectAsState()
    val isLoading by viewModel.isLoading.collectAsState()
    val error by viewModel.error.collectAsState()

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("每日汇总", fontWeight = FontWeight.Bold) },
                actions = { TextButton(onClick = { viewModel.loadAll() }, enabled = !isLoading) { Text("刷新") } },
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
                if (error != null) item { Text(error!!, color = MaterialTheme.colorScheme.error) }
                items(summaries, key = { it.date ?: "" }) { item ->
                    DailyCard(
                        item = item,
                        onClick = { item.date?.let { onDateClick(it) } }
                    )
                }
            }
        }
    }
}

@Composable
private fun DailyCard(
    item: DailyContent,
    onClick: () -> Unit
) {
    val sentiment = com.finapp.appb.ui.components.Sentiment.fromString(item.overallSentiment)
    val score = item.sentimentScore ?: 0.0
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
                if (item.summary != null) Surface(
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

            // 情绪分数
            if (item.sentimentScore != null) Text(
                text = "情绪分数: ${String.format("%.2f", score)}",
                fontSize = 12.sp,
                color = TextSecondary,
                modifier = Modifier.padding(top = 2.dp)
            )

            // 摘要（截断3行）
            if (summary.isNotEmpty()) {
                Text(
                    text = summary,
                    fontSize = 14.sp,
                    color = TextPrimary,
                    maxLines = 3,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.padding(top = 8.dp),
                    lineHeight = 20.sp
                )
            }

        }
    }
}
