package com.finapp.appb.data.local

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.AtomicFile
import java.io.File
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** The credential-management key is separate from the ordinary API key. */
class ManagementKeyStore(context: Context) {
    private val file = AtomicFile(File(context.filesDir, "management-key.bin"))
    private val alias = "finapp-management-key"
    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
        }.generateKey()
    }
    @Synchronized fun save(value: String, server: String) {
        require(value.length in 16..256) { "管理密钥格式不正确" }
        require(server.startsWith("https://")) { "管理授权必须使用 HTTPS 连接" }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
        cipher.updateAAD(server.toByteArray(Charsets.UTF_8))
        val encrypted = cipher.doFinal(value.toByteArray(Charsets.UTF_8))
        val stream = file.startWrite()
        try {
            stream.write(cipher.iv.size); stream.write(cipher.iv); stream.write(encrypted)
            file.finishWrite(stream)
        } catch (error: Exception) { file.failWrite(stream); throw error }
    }
    @Synchronized fun read(server: String): String? = try {
        val bytes = file.openRead().use { it.readBytes() }
        val length = bytes.first().toInt() and 255
        require(length in 12..16 && bytes.size > length + 1)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply {
            init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, bytes.copyOfRange(1, length + 1)))
        }
        cipher.updateAAD(server.toByteArray(Charsets.UTF_8))
        String(cipher.doFinal(bytes.copyOfRange(length + 1, bytes.size)), Charsets.UTF_8)
    } catch (_: Exception) { null }
    @Synchronized fun clear() { file.delete() }
}
