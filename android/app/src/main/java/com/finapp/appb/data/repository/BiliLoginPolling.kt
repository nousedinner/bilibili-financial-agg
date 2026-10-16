package com.finapp.appb.data.repository

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import java.io.IOException

internal fun Throwable.retryableLoginPoll(): Boolean = when (this) {
    is BiliAuthorizationException -> status in listOf(429, 502, 503)
    is ApiException -> status in 500..599
    is IOException -> true
    else -> false
}

/** Keep the same one-use login session through temporary failures, until its deadline. */
internal suspend fun waitForBiliLogin(
    expiresIn: Int,
    poll: suspend () -> Boolean,
    onTemporaryFailure: () -> Unit,
    clock: () -> Long = { System.nanoTime() / 1_000_000 },
    pause: suspend (Long) -> Unit = { delay(it) }
): Boolean {
    val deadline = clock() + expiresIn.coerceIn(1, 180) * 1000L
    while (true) {
        val remaining = deadline - clock()
        if (remaining <= 0) return false
        pause(minOf(5000L, remaining))
        if (clock() >= deadline) return false
        try {
            if (poll()) return true
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (error: Exception) {
            if (!error.retryableLoginPoll()) throw error
            onTemporaryFailure()
        }
    }
}
