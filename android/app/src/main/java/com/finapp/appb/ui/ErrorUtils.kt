package com.finapp.appb.ui

/**
 * 将技术性错误信息转换为用户友好的提示
 */
fun friendlyErrorMessage(error: String?): String {
    if (error == null) return "未知错误"
    return when {
        error.contains("Unable to resolve host", ignoreCase = true) ->
            "无网络连接，请检查网络设置"
        error.contains("timeout", ignoreCase = true) ||
        error.contains("timed out", ignoreCase = true) ->
            "连接超时，请稍后重试"
        error.contains("502") -> "服务暂时不可用，请稍后重试"
        error.contains("503") -> "服务维护中，请稍后重试"
        error.contains("500") -> "服务器错误，请稍后重试"
        error.contains("401") || error.contains("认证", ignoreCase = true) ->
            "认证失败，请重新配置"
        error.contains("加载失败", ignoreCase = true) ->
            "加载失败，请检查网络后重试"
        else -> error
    }
}
