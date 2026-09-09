package com.finapp.appb.ui

/**
 * Feed/Video 时间显示的通用格式化工具
 */
object FormatUtils {
    /** ISO-8601 → "MM-DD HH:mm" */
    fun formatTime(iso: String?): String {
        if (iso.isNullOrBlank()) return ""
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
}
