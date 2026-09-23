package com.epis.epis_mobile

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.database.Cursor
import android.net.Uri
import android.os.Build
import android.provider.ContactsContract
import android.provider.OpenableColumns
import android.provider.Settings
import android.util.Base64
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.ByteArrayOutputStream
import java.security.MessageDigest
import java.util.Locale

class MainActivity : FlutterActivity() {
    companion object {
        private const val CHANNEL = "com.epis.epis_mobile/device"
        private const val REQUEST_READ_CONTACTS = 4101
        private const val REQUEST_CALL_PHONE = 4102
        private const val REQUEST_PICK_ATTACHMENT = 4201
        private const val MAX_ATTACHMENT_BYTES = 5_000_000
    }

    private data class PendingCall(
        val number: String?,
        val contact: String?,
        val result: MethodChannel.Result,
    )

    private data class ContactCandidate(
        val name: String,
        val number: String,
        val isMobile: Boolean,
        val score: Int,
    )

    private sealed class ContactLookup {
        data class Found(val candidate: ContactCandidate) : ContactLookup()
        data class Ambiguous(val names: List<String>) : ContactLookup()
        object NotFound : ContactLookup()
    }

    private var pendingCall: PendingCall? = null
    private var pendingPickerResult: MethodChannel.Result? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            CHANNEL,
        ).setMethodCallHandler { call, result ->
            when (call.method) {
                "getDeviceDescriptor" -> result.success(deviceDescriptor())
                "placeCall" -> startPhoneCall(call.arguments, result)
                "pickAttachment" -> pickAttachment(result)
                else -> result.notImplemented()
            }
        }
    }

    private fun deviceDescriptor(): Map<String, String> {
        val androidId = Settings.Secure.getString(
            contentResolver,
            Settings.Secure.ANDROID_ID,
        ) ?: "epis-android"
        val digest = MessageDigest.getInstance("SHA-256")
            .digest(androidId.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
            .take(24)
        val maker = Build.MANUFACTURER.orEmpty().trim()
        val model = Build.MODEL.orEmpty().trim()
        val label = listOf(maker, model)
            .filter { it.isNotEmpty() }
            .joinToString(" ")
            .ifBlank { "Android Phone" }
        return mapOf(
            "device_id" to "android-$digest",
            "display_name" to label,
        )
    }

    private fun startPhoneCall(arguments: Any?, result: MethodChannel.Result) {
        if (pendingCall != null) {
            result.error("phone_call_busy", "Another phone call request is pending", null)
            return
        }

        val args = arguments as? Map<*, *>
        val number = (args?.get("number") as? String)?.trim().orEmpty()
        val contact = (args?.get("contact") as? String)?.trim().orEmpty()
        if ((number.isEmpty() && contact.isEmpty()) ||
            (number.isNotEmpty() && contact.isNotEmpty())
        ) {
            result.success(
                mapOf(
                    "ok" to false,
                    "error" to "provide_exactly_one_of_number_or_contact",
                ),
            )
            return
        }

        pendingCall = PendingCall(
            number = number.ifEmpty { null },
            contact = contact.ifEmpty { null },
            result = result,
        )
        continuePendingCall()
    }

    private fun continuePendingCall() {
        val pending = pendingCall ?: return
        var targetNumber = pending.number
        var targetLabel: String? = null

        if (pending.contact != null) {
            if (!hasPermission(Manifest.permission.READ_CONTACTS)) {
                requestPermissions(
                    arrayOf(Manifest.permission.READ_CONTACTS),
                    REQUEST_READ_CONTACTS,
                )
                return
            }

            when (val lookup = resolveContact(pending.contact)) {
                ContactLookup.NotFound -> {
                    finishPendingCall(
                        mapOf(
                            "ok" to false,
                            "error" to "contact_not_found",
                            "contact" to pending.contact,
                        ),
                    )
                    return
                }

                is ContactLookup.Ambiguous -> {
                    finishPendingCall(
                        mapOf(
                            "ok" to false,
                            "error" to "contact_ambiguous",
                            "candidates" to lookup.names,
                        ),
                    )
                    return
                }

                is ContactLookup.Found -> {
                    targetNumber = lookup.candidate.number
                    targetLabel = lookup.candidate.name
                }
            }
        }

        val normalized = normalizePhoneNumber(targetNumber.orEmpty())
        if (normalized == null) {
            finishPendingCall(
                mapOf(
                    "ok" to false,
                    "error" to "invalid_phone_number",
                ),
            )
            return
        }

        if (!hasPermission(Manifest.permission.CALL_PHONE)) {
            requestPermissions(
                arrayOf(Manifest.permission.CALL_PHONE),
                REQUEST_CALL_PHONE,
            )
            return
        }

        try {
            val intent = Intent(
                Intent.ACTION_CALL,
                Uri.fromParts("tel", normalized, null),
            )
            startActivity(intent)
            val masked = if (normalized.length <= 4) {
                normalized
            } else {
                "••••${normalized.takeLast(4)}"
            }
            finishPendingCall(
                mapOf(
                    "ok" to true,
                    "status" to "call_intent_started",
                    "target_label" to (targetLabel ?: masked),
                    "call_state_verified" to false,
                ),
            )
        } catch (exc: Exception) {
            finishPendingCall(
                mapOf(
                    "ok" to false,
                    "outcome" to "unknown",
                    "error" to "call_intent_failed:${exc.javaClass.simpleName}",
                ),
            )
        }
    }

    private fun finishPendingCall(payload: Map<String, Any?>) {
        val pending = pendingCall ?: return
        pendingCall = null
        pending.result.success(payload)
    }

    private fun hasPermission(permission: String): Boolean {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.M ||
            checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED
    }

    private fun normalizePhoneNumber(raw: String): String? {
        val cleaned = raw.replace(Regex("""[\s().-]"""), "")
        if (!Regex("^\\+?[0-9]{3,20}$").matches(cleaned)) {
            return null
        }
        return cleaned
    }

    private fun normalizeContactName(value: String): String {
        return value
            .lowercase(Locale("tr", "TR"))
            .replace(Regex("[^a-z0-9çğıöşü]+"), " ")
            .trim()
            .replace(Regex("\\s+"), " ")
    }

    private fun resolveContact(query: String): ContactLookup {
        val normalizedQuery = normalizeContactName(query)
        if (normalizedQuery.isEmpty()) return ContactLookup.NotFound
        val aliases = linkedSetOf(normalizedQuery)
        if (normalizedQuery.length >= 5 && normalizedQuery.endsWith("m")) {
            aliases.add(normalizedQuery.dropLast(1))
        }

        val projection = arrayOf(
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME_PRIMARY,
            ContactsContract.CommonDataKinds.Phone.NUMBER,
            ContactsContract.CommonDataKinds.Phone.TYPE,
        )
        val candidates = mutableListOf<ContactCandidate>()

        val cursor: Cursor? = contentResolver.query(
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
            projection,
            null,
            null,
            null,
        )
        cursor?.use {
            val nameIndex = it.getColumnIndex(
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME_PRIMARY,
            )
            val numberIndex = it.getColumnIndex(
                ContactsContract.CommonDataKinds.Phone.NUMBER,
            )
            val typeIndex = it.getColumnIndex(
                ContactsContract.CommonDataKinds.Phone.TYPE,
            )
            while (it.moveToNext() && candidates.size < 100) {
                val name = if (nameIndex >= 0) it.getString(nameIndex).orEmpty() else ""
                val number = if (numberIndex >= 0) it.getString(numberIndex).orEmpty() else ""
                val normalizedName = normalizeContactName(name)
                if (normalizedName.isEmpty()) continue

                var bestScore: Int? = null
                for (alias in aliases) {
                    val score = when {
                        normalizedName == alias -> 0
                        normalizedName.startsWith(alias) -> 1
                        normalizedName.contains(alias) -> 2
                        else -> null
                    }
                    if (score != null && (bestScore == null || score < bestScore)) {
                        bestScore = score
                    }
                }
                val score = bestScore ?: continue
                val normalizedNumber = normalizePhoneNumber(number) ?: continue
                val type = if (typeIndex >= 0) it.getInt(typeIndex) else -1
                candidates.add(
                    ContactCandidate(
                        name = name.trim(),
                        number = normalizedNumber,
                        isMobile = type == ContactsContract.CommonDataKinds.Phone.TYPE_MOBILE,
                        score = score,
                    ),
                )
            }
        }

        if (candidates.isEmpty()) return ContactLookup.NotFound
        val bestScore = candidates.minOf { it.score }
        val best = candidates.filter { it.score == bestScore }
        val distinctNames = best.map { it.name }.distinct()
        if (distinctNames.size == 1) {
            val mobile = best.filter { it.isMobile }.distinctBy { it.number }
            val chosen = when {
                mobile.size == 1 -> mobile.first()
                best.distinctBy { it.number }.size == 1 -> best.first()
                else -> null
            }
            if (chosen != null) return ContactLookup.Found(chosen)
        }

        return ContactLookup.Ambiguous(
            distinctNames.take(8),
        )
    }

    private fun pickAttachment(result: MethodChannel.Result) {
        if (pendingPickerResult != null) {
            result.error("attachment_picker_busy", "Attachment picker already open", null)
            return
        }
        pendingPickerResult = result
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "*/*"
            putExtra(
                Intent.EXTRA_MIME_TYPES,
                arrayOf(
                    "image/*",
                    "text/*",
                    "application/json",
                    "application/xml",
                    "application/javascript",
                    "application/octet-stream",
                ),
            )
        }
        startActivityForResult(intent, REQUEST_PICK_ATTACHMENT)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQUEST_PICK_ATTACHMENT) return
        val result = pendingPickerResult ?: return
        pendingPickerResult = null

        if (resultCode != Activity.RESULT_OK || data?.data == null) {
            result.success(null)
            return
        }

        val uri = data.data ?: run {
            result.success(null)
            return
        }

        try {
            val metadata = attachmentMetadata(uri)
            val bytes = readAttachmentBytes(uri)
            result.success(
                mapOf(
                    "name" to metadata.first,
                    "mime_type" to metadata.second,
                    "size_bytes" to bytes.size,
                    "data_base64" to Base64.encodeToString(bytes, Base64.NO_WRAP),
                ),
            )
        } catch (exc: IllegalArgumentException) {
            result.error("attachment_too_large", exc.message, null)
        } catch (exc: Exception) {
            result.error(
                "attachment_read_failed",
                exc.javaClass.simpleName,
                null,
            )
        }
    }

    private fun attachmentMetadata(uri: Uri): Pair<String, String> {
        var name = "attachment"
        contentResolver.query(
            uri,
            arrayOf(OpenableColumns.DISPLAY_NAME),
            null,
            null,
            null,
        )?.use { cursor ->
            if (cursor.moveToFirst()) {
                val index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (index >= 0) {
                    name = cursor.getString(index).orEmpty().trim().ifBlank { name }
                }
            }
        }
        val mime = contentResolver.getType(uri).orEmpty().ifBlank {
            "application/octet-stream"
        }
        return Pair(name.take(180), mime.take(120))
    }

    private fun readAttachmentBytes(uri: Uri): ByteArray {
        val output = ByteArrayOutputStream()
        val buffer = ByteArray(32 * 1024)
        contentResolver.openInputStream(uri)?.use { input ->
            while (true) {
                val read = input.read(buffer)
                if (read < 0) break
                output.write(buffer, 0, read)
                if (output.size() > MAX_ATTACHMENT_BYTES) {
                    throw IllegalArgumentException("Dosya 5 MB sınırını aşıyor.")
                }
            }
        } ?: throw IllegalStateException("attachment_stream_unavailable")
        return output.toByteArray()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_READ_CONTACTS && requestCode != REQUEST_CALL_PHONE) {
            return
        }
        val granted = grantResults.isNotEmpty() &&
            grantResults[0] == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            val error = if (requestCode == REQUEST_READ_CONTACTS) {
                "contacts_permission_denied"
            } else {
                "call_permission_denied"
            }
            finishPendingCall(mapOf("ok" to false, "error" to error))
            return
        }
        continuePendingCall()
    }
}
