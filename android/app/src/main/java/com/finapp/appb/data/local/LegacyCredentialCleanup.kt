package com.finapp.appb.data.local

import android.content.Context
import android.util.AtomicFile
import android.util.Log
import java.io.File
import java.security.KeyStore

/** Remove the retired second credential without touching the connection API key. */
fun removeLegacyManagementKey(context: Context) {
    try {
        AtomicFile(File(context.filesDir, "management-key.bin")).delete()
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        if (store.containsAlias("finapp-management-key")) store.deleteEntry("finapp-management-key")
    } catch (_: Exception) {
        Log.w("FinApp", "Retired credential cleanup will be retried on next startup")
    }
}
