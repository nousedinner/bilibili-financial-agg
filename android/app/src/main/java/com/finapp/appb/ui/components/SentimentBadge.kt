package com.finapp.appb.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.finapp.appb.ui.theme.BearishGreen
import com.finapp.appb.ui.theme.BullishRed
import com.finapp.appb.ui.theme.NeutralGray

enum class Sentiment(val label: String, val icon: String, val color: Color) {
    BULLISH("看多", "▲", BullishRed),
    BEARISH("看空", "▼", BearishGreen),
    NEUTRAL("中性", "●", NeutralGray);

    companion object {
        fun fromString(s: String?): Sentiment = when (s?.lowercase()) {
            "bullish" -> BULLISH
            "bearish" -> BEARISH
            else -> NEUTRAL
        }
    }
}

@Composable
fun SentimentBadge(sentiment: String?, modifier: Modifier = Modifier) {
    val s = Sentiment.fromString(sentiment)
    Row(
        modifier = modifier,
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(2.dp)
    ) {
        Text(s.icon, color = s.color, fontSize = 11.sp)
        Text(s.label, color = s.color, fontSize = 12.sp, fontWeight = FontWeight.Medium)
    }
}

@Composable
fun SentimentBar(score: Double?, modifier: Modifier = Modifier) {
    val s = score ?: 0.0
    val color = when {
        s > 0.3 -> BullishRed
        s < -0.3 -> BearishGreen
        else -> NeutralGray
    }
    val fraction = ((s + 1.0) / 2.0).coerceIn(0.0, 1.0).toFloat()

    Box(modifier = modifier.height(4.dp).clip(RoundedCornerShape(2.dp)).background(Color.LightGray)) {
        Box(
            modifier = Modifier
                .fillMaxHeight()
                .fillMaxWidth(fraction)
                .clip(RoundedCornerShape(2.dp))
                .background(color)
        )
    }
}
