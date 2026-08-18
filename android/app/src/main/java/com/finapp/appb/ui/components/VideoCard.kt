package com.finapp.appb.ui.components

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.finapp.appb.data.api.FeedItem
import com.finapp.appb.ui.theme.*

@Composable
fun VideoCard(
    item: FeedItem,
    bloggerNames: Map<Long, String>,
    sentimentScore: Double = 0.0,
    onClick: () -> Unit
) {
    val title = item.title ?: item.summary ?: ""
    val summary = item.summary ?: ""
    val publishTime = item.publishTime ?: ""
    val sentiment = item.sentiment ?: "neutral"
    val isVideo = item.type == "video"
    val isDynamic = item.type == "dynamic"
    val bloggerName = bloggerNames[item.mid] ?: "博主 #${item.mid ?: "?"}"

    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 6.dp)
            .then(
                if (isVideo) Modifier.clickable(onClick = onClick)
                else Modifier
            ),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(
            containerColor = if (isDynamic) SurfaceVariant else CardBackground
        ),
        elevation = CardDefaults.cardElevation(defaultElevation = 1.dp)
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            // 博主 + 时间
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = bloggerName,
                    fontSize = 13.sp,
                    color = TextSecondary
                )
                Row(
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    if (isDynamic) {
                        AssistChip(
                            onClick = {},
                            label = { Text("动态", fontSize = 10.sp) },
                            shape = RoundedCornerShape(6.dp)
                        )
                    }
                    Text(
                        text = formatTime(publishTime),
                        fontSize = 12.sp,
                        color = TextSecondary
                    )
                }
            }

            Spacer(modifier = Modifier.height(8.dp))

            // 标题
            Text(
                text = title,
                fontSize = 16.sp,
                fontWeight = FontWeight.SemiBold,
                color = TextPrimary,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis
            )

            // 摘要 (only show if different from title)
            if (summary.isNotBlank() && summary != title) {
                Spacer(modifier = Modifier.height(6.dp))
                Text(
                    text = summary,
                    fontSize = 13.sp,
                    color = TextSecondary,
                    maxLines = 3,
                    overflow = TextOverflow.Ellipsis,
                    lineHeight = 18.sp
                )
            }

            Spacer(modifier = Modifier.height(8.dp))

            // 情绪标签 + 分数条
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                SentimentBadge(sentiment = sentiment)
                SentimentBar(
                    score = sentimentScore,
                    modifier = Modifier.weight(1f)
                )
            }
        }
    }
}

private fun formatTime(iso: String): String {
    if (iso.isBlank()) return ""
    return try {
        val parts = iso.split("T")
        if (parts.size >= 2) {
            val time = parts[1].take(5)
            "${parts[0].takeLast(5)} $time"
        } else iso.take(16)
    } catch (_: Exception) {
        iso.take(16)
    }
}
