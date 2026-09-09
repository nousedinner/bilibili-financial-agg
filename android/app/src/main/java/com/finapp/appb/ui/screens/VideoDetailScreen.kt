@file:OptIn(ExperimentalLayoutApi::class)

package com.finapp.appb.ui.screens

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.OpenInBrowser
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.finapp.appb.ui.FormatUtils
import com.finapp.appb.ui.components.SentimentBadge
import com.finapp.appb.ui.components.SkeletonDetail
import com.finapp.appb.ui.friendlyErrorMessage
import com.finapp.appb.ui.theme.*
import com.finapp.appb.viewmodel.VideoDetailViewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun VideoDetailScreen(
    bvid: String,
    viewModel: VideoDetailViewModel,
    onBack: () -> Unit
) {
    val context = LocalContext.current
    val detail by viewModel.detail.collectAsState()
    val isLoading by viewModel.isLoading.collectAsState()
    val error by viewModel.error.collectAsState()

    LaunchedEffect(bvid) { viewModel.loadDetail(bvid) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("视频详情", fontSize = 16.sp) },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.background),
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, "返回")
                    }
                },
                actions = {
                    IconButton(onClick = { viewModel.loadDetail(bvid) }) {
                        Icon(Icons.Default.Refresh, "刷新")
                    }
                    IconButton(onClick = {
                        try {
                            // 优先用B站APP打开
                            val intent = Intent(Intent.ACTION_VIEW, Uri.parse("bilibili://video/$bvid"))
                            intent.setPackage("tv.danmaku.bili")
                            context.startActivity(intent)
                        } catch (_: Exception) {
                            // 没装B站APP，fallback到浏览器
                            context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("https://www.bilibili.com/video/$bvid")))
                        }
                    }) {
                        Icon(Icons.Default.OpenInBrowser, "在B站打开")
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
                    Button(onClick = { viewModel.loadDetail(bvid) }) { Text("重试") }
                }
            }
            detail != null -> {
                val d = detail!!
                val title = d.title ?: ""
                val summaryText = d.summary?.text ?: ""
                val keyPoints = d.summary?.keyPoints ?: emptyList()
                val riskWarnings = d.summary?.riskWarnings ?: emptyList()
                val dataCitations = d.summary?.dataCitations ?: emptyList()
                val tags = d.summary?.tags ?: emptyList()
                val sentiment = d.summary?.sentiment ?: "neutral"

                Column(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(padding)
                        .verticalScroll(rememberScrollState())
                        .padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(16.dp)
                ) {
                    if (d.fromCache) Text("网络暂不可用，当前显示缓存详情", color = MaterialTheme.colorScheme.error)

                    // Header
                    Text(title, fontSize = 20.sp, fontWeight = FontWeight.Bold, lineHeight = 26.sp)
                    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        Text("👁 ${d.viewCount ?: 0}", fontSize = 13.sp, color = TextSecondary)
                        Text("⏱ ${formatDuration(d.duration ?: 0)}", fontSize = 13.sp, color = TextSecondary)
                        Text(FormatUtils.formatTime(d.publishTime ?: ""), fontSize = 13.sp, color = TextSecondary)
                    }

                    // AI Analysis
                    if (summaryText.isNotBlank()) {
                        SectionCard(title = "🤖 AI 分析") {
                            Text(summaryText, fontSize = 14.sp, lineHeight = 20.sp)

                            if (keyPoints.isNotEmpty()) {
                                Spacer(modifier = Modifier.height(10.dp))
                                Text("📌 关键观点", fontWeight = FontWeight.SemiBold, fontSize = 13.sp)
                                keyPoints.forEach { pt ->
                                    Text("• $pt", fontSize = 13.sp, color = TextSecondary, lineHeight = 18.sp)
                                }
                            }

                            if (riskWarnings.isNotEmpty()) {
                                Spacer(modifier = Modifier.height(10.dp))
                                Text("⚠️ 风险提示", fontWeight = FontWeight.SemiBold, fontSize = 13.sp, color = WarningRed)
                                riskWarnings.forEach { w ->
                                    Text("• $w", fontSize = 13.sp, color = WarningRed, lineHeight = 18.sp)
                                }
                            }

                            if (dataCitations.isNotEmpty()) {
                                Spacer(modifier = Modifier.height(10.dp))
                                Text("📊 数据引用", fontWeight = FontWeight.SemiBold, fontSize = 13.sp, color = InfoBlue)
                                dataCitations.forEach { c ->
                                    Text("• $c", fontSize = 13.sp, color = InfoBlue, lineHeight = 18.sp)
                                }
                            }

                            if (tags.isNotEmpty()) {
                                Spacer(modifier = Modifier.height(10.dp))
                                val validTags = tags.filter { tag ->
                                    tag.isNotBlank() && tag.trim().isNotEmpty() && tag.length > 1
                                }.take(5)
                                if (validTags.isNotEmpty()) {
                                    FlowRow(
                                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                                        verticalArrangement = Arrangement.spacedBy(6.dp)
                                    ) {
                                        validTags.forEach { tag ->
                                            AssistChip(
                                                onClick = {},
                                                label = { Text(tag, fontSize = 12.sp) },
                                                shape = RoundedCornerShape(8.dp)
                                            )
                                        }
                                    }
                                }
                            }

                            Spacer(modifier = Modifier.height(8.dp))
                            SentimentBadge(sentiment = sentiment)
                        }
                    }

                    // Comment Analysis
                    d.comments?.let { ca ->
                        SectionCard(title = "💬 评论分析") {
                            val sent = ca.sentiment
                            if (sent != null) {
                                Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                                    SentimentPct("看多", sent.bullish ?: 0.0, BullishRed)
                                    SentimentPct("看空", sent.bearish ?: 0.0, BearishGreen)
                                    SentimentPct("中性", sent.neutral ?: 0.0, NeutralGray)
                                }
                            }
                            Spacer(modifier = Modifier.height(8.dp))
                            Text("共 ${ca.total ?: 0} 条评论", fontSize = 12.sp, color = TextSecondary)

                            val hotComments = ca.hotComments ?: emptyList()
                            if (hotComments.isNotEmpty()) {
                                Spacer(modifier = Modifier.height(10.dp))
                                Text("热门评论", fontWeight = FontWeight.SemiBold, fontSize = 13.sp)
                                hotComments.take(5).forEach { comment ->
                                    Card(
                                        modifier = Modifier.fillMaxWidth().padding(vertical = 3.dp),
                                        shape = RoundedCornerShape(8.dp),
                                        colors = CardDefaults.cardColors(containerColor = SurfaceVariant)
                                    ) {
                                        Text(
                                            comment,
                                            modifier = Modifier.padding(10.dp),
                                            fontSize = 13.sp,
                                            lineHeight = 18.sp
                                        )
                                    }
                                }
                            }

                            val keywords = ca.keywords ?: emptyList()
                            if (keywords.isNotEmpty()) {
                                Spacer(modifier = Modifier.height(8.dp))
                                Text("关键词: ${keywords.joinToString("  ")}", fontSize = 12.sp, color = TextSecondary)
                            }
                        }
                    }

                    // Danmaku Analysis
                    d.danmaku?.let { da ->
                        SectionCard(title = "🎯 弹幕分析") {
                            val sent = da.sentiment
                            if (sent != null) {
                                Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                                    SentimentPct("看多", sent.bullish ?: 0.0, BullishRed)
                                    SentimentPct("看空", sent.bearish ?: 0.0, BearishGreen)
                                    SentimentPct("中性", sent.neutral ?: 0.0, NeutralGray)
                                }
                            }
                            Spacer(modifier = Modifier.height(8.dp))
                            Text("采样 ${da.sampled ?: 0}/${da.total ?: 0} 条", fontSize = 12.sp, color = TextSecondary)
                            val keywords = da.keywords ?: emptyList()
                            if (keywords.isNotEmpty()) {
                                Spacer(modifier = Modifier.height(6.dp))
                                Text("关键词: ${keywords.joinToString("  ")}", fontSize = 12.sp, color = TextSecondary)
                            }
                        }
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    Button(
                        onClick = {
                            val url = "https://www.bilibili.com/video/$bvid"
                            context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                        },
                        modifier = Modifier.fillMaxWidth().height(48.dp),
                        shape = RoundedCornerShape(12.dp)
                    ) {
                        Text("在 B站 观看完整视频", fontSize = 15.sp)
                    }

                    Spacer(modifier = Modifier.height(32.dp))
                }
            }
        }
    }
}

@Composable
private fun SectionCard(title: String, content: @Composable ColumnScope.() -> Unit) {
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

@Composable
private fun SentimentPct(label: String, value: Double, color: androidx.compose.ui.graphics.Color) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Text("${(value * 100).toInt()}%", fontSize = 18.sp, fontWeight = FontWeight.Bold, color = color)
        Text(label, fontSize = 11.sp, color = TextSecondary)
    }
}

private fun formatDuration(seconds: Int): String {
    val m = seconds / 60
    val s = seconds % 60
    return "${m}:${String.format("%02d", s)}"
}
