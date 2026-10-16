package com.finapp.appb.notifications

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.*
import com.finapp.appb.BuildConfig
import com.finapp.appb.FinApp
import com.finapp.appb.MainActivity
import com.finapp.appb.R
import com.finapp.appb.data.api.AuthorizationStatus
import com.finapp.appb.data.api.ServerAlert
import com.google.firebase.FirebaseApp
import com.google.firebase.FirebaseOptions
import com.google.firebase.messaging.FirebaseMessaging
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.CancellationException
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume

object AuthorizationNotifications {
    private const val channel = "server_auth"
    private const val tag = "bili_auth"
    private fun prefs(context: Context) = context.getSharedPreferences("authorization_notifications", Context.MODE_PRIVATE)
    fun enabled(context: Context) = prefs(context).getBoolean("enabled", false)
    fun permitted(context: Context) = NotificationManagerCompat.from(context).areNotificationsEnabled() &&
        (Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED)
    fun pushConfigured() = listOf(BuildConfig.FIREBASE_APP_ID, BuildConfig.FIREBASE_API_KEY,
        BuildConfig.FIREBASE_PROJECT_ID, BuildConfig.FIREBASE_SENDER_ID).all { it.isNotBlank() }

    private fun channel(context: Context) {
        context.getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(channel, "服务器授权告警", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "服务器 B 站授权失效、恢复及送达测试"
            })
    }
    private fun initializeFirebase(context: Context): Boolean = if (!pushConfigured()) false else try {
        if (FirebaseApp.getApps(context).isEmpty()) {
            FirebaseApp.initializeApp(context, FirebaseOptions.Builder()
                .setApplicationId(BuildConfig.FIREBASE_APP_ID).setApiKey(BuildConfig.FIREBASE_API_KEY)
                .setProjectId(BuildConfig.FIREBASE_PROJECT_ID).setGcmSenderId(BuildConfig.FIREBASE_SENDER_ID).build())
        }
        true
    } catch (_: Exception) { false }
    fun enable(context: Context) {
        prefs(context).edit().putBoolean("enabled", true).apply()
        start(context)
    }
    fun start(context: Context) {
        if (!enabled(context)) return
        channel(context)
        initializeFirebase(context)
        val work = PeriodicWorkRequestBuilder<AuthorizationRefreshWorker>(15, TimeUnit.MINUTES)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()).build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork("authorization_status_refresh", ExistingPeriodicWorkPolicy.KEEP, work)
        (context.applicationContext as FinApp).appScope.launch { synchronize(context) }
    }
    suspend fun disable(context: Context) {
        prefs(context).edit().putBoolean("enabled",false).apply()
        WorkManager.getInstance(context).cancelUniqueWork("authorization_status_refresh")
        NotificationManagerCompat.from(context).cancel(tag,0)
        val token=prefs(context).getString("token",null)
        if(token!=null) (context.applicationContext as FinApp).repository.registerPush(token,false)
    }
    private suspend fun registrationToken(context: Context): String? {
        if (!initializeFirebase(context)) return null
        return suspendCancellableCoroutine { continuation ->
            FirebaseMessaging.getInstance().token.addOnSuccessListener { if (continuation.isActive) continuation.resume(it) }
                .addOnFailureListener { if (continuation.isActive) continuation.resume(null) }
        }
    }
    fun rememberToken(context: Context, token: String) {
        prefs(context).edit().putString("token", token).apply()
    }
    suspend fun synchronize(context: Context): Boolean {
        val app = context.applicationContext as FinApp
        if (!enabled(context) || !app.repository.ensureInitialized()) return true
        val token = withTimeoutOrNull(10000) { registrationToken(context) } ?: prefs(context).getString("token", null)
        if (token != null) {
            rememberToken(context, token)
            app.repository.registerPush(token, permitted(context))
        }
        val status = app.repository.authorizationStatus().getOrNull() ?: return false
        val events = app.repository.serverAlerts().getOrNull() ?: return false
        // Periodic work is a delayed fallback; it never claims an FCM test arrived.
        for (event in events.asReversed()) {
            if (event.kind != "test" && !event.acknowledged && event.state != "cancelled") show(context, event, status)
        }
        if (status.state == "valid" && events.none { it.kind == "recovered" && !it.acknowledged }) {
            NotificationManagerCompat.from(context).cancel(tag, 0)
        }
        return true
    }
    suspend fun show(context: Context, event: ServerAlert, status: AuthorizationStatus?) {
        if (!enabled(context) || !permitted(context)) return
        if (event.kind in listOf("invalid", "reminder") && status != null &&
            (status.state == "valid" || status.credentialVersion != event.credentialVersion)) return
        if (event.kind == "recovered" && status != null &&
            (status.state != "valid" || status.credentialVersion != event.credentialVersion)) return
        val app = context.applicationContext as FinApp
        val connection = app.preferences.config.first()
        val scope = MessageDigest.getInstance("SHA-256").digest((connection.url + "\n" + connection.key).toByteArray())
            .joinToString("") { "%02x".format(it) }
        val seenKey = "$scope:${event.id}"
        if (prefs(context).getBoolean(seenKey, false)) return
        channel(context)
        val intent = Intent(context, MainActivity::class.java).putExtra("route", "bili_auth").putExtra("event_id", event.id)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        val action = PendingIntent.getActivity(context, event.id.hashCode(), intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val title = when (event.kind) { "test" -> "财经聚合通知测试"; "recovered" -> "服务器 B 站授权已恢复"; else -> "服务器 B 站授权需要处理" }
        val body = if (event.kind == "test") "点击确认已收到服务器推送" else if (event.kind == "recovered") "授权已恢复，可继续采集" else "点击查看服务器授权状态并重新授权"
        val notification = NotificationCompat.Builder(context, channel).setSmallIcon(R.drawable.ic_stat_server_auth)
            .setContentTitle(title).setContentText(body).setContentIntent(action)
            .setOnlyAlertOnce(true).setAutoCancel(true).build()
        if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(context,
                Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        try {
            NotificationManagerCompat.from(context).notify(tag, 0, notification)
        } catch (_: SecurityException) {
            // Permission can be revoked between checking and posting.
            return
        }
        prefs(context).edit().putBoolean(seenKey, true).apply()
    }
}

class AuthorizationRefreshWorker(context: Context, parameters: WorkerParameters) : CoroutineWorker(context, parameters) {
    override suspend fun doWork(): Result = try {
        if (AuthorizationNotifications.synchronize(applicationContext)) Result.success() else Result.retry()
    } catch (cancelled: CancellationException) { throw cancelled } catch (_: Exception) { Result.retry() }
}

// Keep network work within WorkManager's lifecycle. Firebase may stop the
// service immediately after onMessageReceived returns.
class AuthorizationPushWorker(context: Context, parameters: WorkerParameters) : CoroutineWorker(context, parameters) {
    override suspend fun doWork(): Result {
        return try {
        val eventId = inputData.getString("event_id") ?: return Result.success()
        if (!AuthorizationNotifications.enabled(applicationContext) || !AuthorizationNotifications.permitted(applicationContext)) {
            return Result.success()
        }
        val app = applicationContext as FinApp
        if (!app.repository.ensureInitialized()) return Result.success()
        val status = app.repository.authorizationStatus().getOrNull() ?: return Result.retry()
        val events = app.repository.serverAlerts().getOrNull() ?: return Result.retry()
        val event = events.firstOrNull { it.id == eventId } ?: return Result.success()
        if (event.acknowledged || event.state == "cancelled") return Result.success()
        // A test is only shown after the provider accepted the real FCM send;
        // periodic refresh deliberately never displays test events.
        if (event.kind == "test" && event.state != "submitted") return Result.retry()
        AuthorizationNotifications.show(applicationContext, event, status)
        Result.success()
        } catch (cancelled: CancellationException) { throw cancelled } catch (_: Exception) { Result.retry() }
    }
}

class FinMessagingService : FirebaseMessagingService() {
    override fun onNewToken(token: String) {
        AuthorizationNotifications.rememberToken(this, token)
        if (!AuthorizationNotifications.enabled(this)) return
        val app = applicationContext as FinApp
        app.appScope.launch {
            if (app.repository.ensureInitialized()) app.repository.registerPush(token, AuthorizationNotifications.permitted(this@FinMessagingService))
        }
    }
    override fun onMessageReceived(message: RemoteMessage) {
        val id = message.data["event_id"] ?: return
        val kind = message.data["kind"] ?: return
        if (kind !in listOf("invalid", "reminder", "recovered", "test")) return
        val builder = OneTimeWorkRequestBuilder<AuthorizationPushWorker>()
            .setInputData(workDataOf("event_id" to id))
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 10, TimeUnit.SECONDS)
        if (Build.VERSION.SDK_INT >= 31) builder.setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
        WorkManager.getInstance(this).enqueueUniqueWork("authorization_push:$id", ExistingWorkPolicy.KEEP, builder.build())
    }
}
