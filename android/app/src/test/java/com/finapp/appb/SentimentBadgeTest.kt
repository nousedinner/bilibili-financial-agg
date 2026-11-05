package com.finapp.appb

import com.finapp.appb.ui.components.Sentiment
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 徽章口径：与 docs/API_REFERENCE.md「情绪说明」一致 —
 * bullish 0.3~1.0，neutral -0.3~0.3，bearish -1.0~-0.3。
 */
class SentimentBadgeTest {

    @Test
    fun `score above 0 3 maps to bullish`() {
        assertEquals(Sentiment.BULLISH, Sentiment.fromScore(0.3))
        assertEquals(Sentiment.BULLISH, Sentiment.fromScore(0.05 + 0.99))
        assertEquals(Sentiment.BULLISH, Sentiment.fromScore(1.0))
    }

    @Test
    fun `score inside neutral band maps to neutral`() {
        assertEquals(Sentiment.NEUTRAL, Sentiment.fromScore(0.29))
        assertEquals(Sentiment.NEUTRAL, Sentiment.fromScore(0.05))  // 11-02 实测值
        assertEquals(Sentiment.NEUTRAL, Sentiment.fromScore(0.03))  // 11-04 实测值
        assertEquals(Sentiment.NEUTRAL, Sentiment.fromScore(-0.27)) // 11-01 实测值
        assertEquals(Sentiment.NEUTRAL, Sentiment.fromScore(0.0))
    }

    @Test
    fun `score at or below -0 3 maps to bearish`() {
        assertEquals(Sentiment.BEARISH, Sentiment.fromScore(-0.3))
        assertEquals(Sentiment.BEARISH, Sentiment.fromScore(-0.82))
        assertEquals(Sentiment.BEARISH, Sentiment.fromScore(-1.0))
    }

    @Test
    fun `missing score falls back to llm label`() {
        assertEquals(Sentiment.BEARISH, Sentiment.fromScore(null, "bearish"))
        assertEquals(Sentiment.BULLISH, Sentiment.fromScore(null, "BULLISH"))
        assertEquals(Sentiment.NEUTRAL, Sentiment.fromScore(null, null))
        assertEquals(Sentiment.NEUTRAL, Sentiment.fromScore(null, "garbage"))
    }
}
