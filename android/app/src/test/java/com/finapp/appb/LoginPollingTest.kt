package com.finapp.appb

import com.finapp.appb.data.repository.ManagementException
import com.finapp.appb.data.repository.waitForBiliLogin
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test
import java.io.IOException

class LoginPollingTest {
    @Test fun temporaryExchangeFailureKeepsPollingTheSameSession() = runBlocking {
        var elapsed = 0L
        var attempts = 0
        var notices = 0
        val complete = waitForBiliLogin(180, poll = {
            when (++attempts) {
                1 -> throw ManagementException("temporary exchange",503)
                2 -> throw IOException("network interrupted")
                else -> true
            }
        }, onTemporaryFailure = { notices++ }, clock = { elapsed }, pause = { elapsed += it })
        assertTrue(complete)
        assertEquals(3,attempts)
        assertEquals(2,notices)
        assertEquals(15000L,elapsed)
    }

    @Test fun slowRequestsConsumeTheQrLifetimeInsteadOfExtendingItPerAttempt() = runBlocking {
        var elapsed = 0L
        var attempts = 0
        val complete = waitForBiliLogin(20, poll = {
            attempts++
            elapsed += 40000L
            throw IOException("timeout")
        }, onTemporaryFailure = {}, clock = { elapsed }, pause = { elapsed += it })
        assertFalse(complete)
        assertEquals(1,attempts)
        assertEquals(45000L,elapsed)
    }

    @Test fun wrongManagementKeyEndsTheAttemptWithoutAutomaticRetry() = runBlocking {
        var elapsed = 0L
        var attempts = 0
        try {
            waitForBiliLogin(180,poll = { attempts++; throw ManagementException("wrong key",403) },
                onTemporaryFailure = { fail("Permanent authorization errors must not retry") },
                clock = { elapsed },pause = { elapsed += it })
            fail("Expected the authorization error")
        } catch (error: ManagementException) {
            assertEquals(403,error.status)
            assertEquals(1,attempts)
        }
    }

    @Test fun leavingTheFlowPropagatesCancellation() = runBlocking {
        var elapsed = 0L
        try {
            waitForBiliLogin(180,poll = { throw CancellationException("screen closed") },
                onTemporaryFailure = { fail("Cancellation must not retry") },
                clock = { elapsed },pause = { elapsed += it })
            fail("Expected cancellation")
        } catch (_: CancellationException) { }
    }
}
