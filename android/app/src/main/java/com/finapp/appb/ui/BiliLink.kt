package com.finapp.appb.ui

import android.content.Context
import android.content.Intent
import android.net.Uri

/**
 * 统一B站跳转工具
 *
 * 国内版/国际版 都通过 https:// web URL 打开视频和空间，
 * 系统会自动选择已安装的B站APP处理（无需指定包名）。
 * 无B站APP时走浏览器。
 */
object BiliLink {
    /** 在B站APP打开视频 */
    fun openVideo(context: Context, bvid: String) {
        val uri = Uri.parse("https://www.bilibili.com/video/$bvid")
        openUri(context, uri)
    }

    /** 在B站APP打开UP主空间 */
    fun openSpace(context: Context, mid: Long) {
        val uri = Uri.parse("https://space.bilibili.com/$mid")
        openUri(context, uri)
    }

    private fun openUri(context: Context, uri: Uri) {
        try {
            context.startActivity(Intent(Intent.ACTION_VIEW, uri).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            })
        } catch (_: Exception) {}
    }
}
