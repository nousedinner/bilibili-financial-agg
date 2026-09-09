package com.finapp.appb.ui

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri

/**
 * 统一B站跳转工具：国内版 → 浏览器兜底
 *
 * 国内版(tv.danmaku.bili)支持 bilibili:// 深链接
 * 国际版(com.bilibili.app.in)不支持视频/空间深链接，直接走浏览器
 */
object BiliLink {
    private const val PKG_DOMESTIC = "tv.danmaku.bili"

    /** 在B站APP打开视频 */
    fun openVideo(context: Context, bvid: String) {
        val webUri = Uri.parse("https://www.bilibili.com/video/$bvid")
        val domesticUri = Uri.parse("bilibili://video/$bvid")

        if (isAppInstalled(context, PKG_DOMESTIC)) {
            try {
                context.startActivity(Intent(Intent.ACTION_VIEW, domesticUri).apply {
                    setPackage(PKG_DOMESTIC)
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                })
                return
            } catch (_: Exception) {}
        }
        // 浏览器兜底（国际版也走这里）
        try {
            context.startActivity(Intent(Intent.ACTION_VIEW, webUri).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            })
        } catch (_: Exception) {}
    }

    /** 在B站APP打开UP主空间 */
    fun openSpace(context: Context, mid: Long) {
        val webUri = Uri.parse("https://space.bilibili.com/$mid")
        val domesticUri = Uri.parse("bilibili://space/$mid")

        if (isAppInstalled(context, PKG_DOMESTIC)) {
            try {
                context.startActivity(Intent(Intent.ACTION_VIEW, domesticUri).apply {
                    setPackage(PKG_DOMESTIC)
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                })
                return
            } catch (_: Exception) {}
        }
        // 浏览器兜底
        try {
            context.startActivity(Intent(Intent.ACTION_VIEW, webUri).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            })
        } catch (_: Exception) {}
    }

    private fun isAppInstalled(context: Context, packageName: String): Boolean = try {
        context.packageManager.getPackageInfo(packageName, 0)
        true
    } catch (_: PackageManager.NameNotFoundException) { false }
}
