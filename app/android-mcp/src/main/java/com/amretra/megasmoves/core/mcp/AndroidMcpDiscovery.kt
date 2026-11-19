package com.amretra.megasmoves.core.mcp

import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import org.json.JSONTokener
import java.util.concurrent.TimeUnit

/** Synchronous Android MCP tool handler. Invoke it on the authenticated transport's worker thread. */
class AndroidMcpDiscovery internal constructor(
    private val selectedWorker: () -> SelectedWorker?,
    transport: OkHttpClient,
) {
    constructor(selectedWorker: () -> SelectedWorker?) : this(selectedWorker, OkHttpClient())

    private val client = transport.newBuilder()
        .followRedirects(false)
        .followSslRedirects(false)
        .retryOnConnectionFailure(false)
        .callTimeout(10, TimeUnit.SECONDS)
        .build()

    fun handle(toolName: String, argumentsJson: String): String {
        if (toolName != "android_chitraagni_tools") return failure("unknown_android_tool")
        if (argumentsJson.length > 64 * 1024) return failure("arguments_too_large")
        val arguments = try {
            val parser = JSONTokener(argumentsJson)
            val args = parser.nextValue() as JSONObject
            require(parser.nextClean() == '\u0000')
            require(args.keys().asSequence().all { it == "offset" || it == "limit" })
            for ((key, range) in listOf("offset" to 0..10_000, "limit" to 1..5)) {
                if (args.has(key)) require(args.get(key) is Int && args.getInt(key) in range)
            }
            args
        } catch (_: Exception) {
            return failure("arguments_invalid")
        }
        return try {
            val selected = selectedWorker() ?: return failure("chitraagni_pairing_required")
            val endpoint = selected.endpoint
            val key = workerSelectionKey(selected.ownerUid, endpoint, selected.pairing)
            if (key == null || key != selected.selectedKey || !selected.isCurrent()) {
                return failure("chitraagni_pairing_required")
            }
            val credential = selected.readCredential()?.takeIf { it.length in 1..4096 && it.all { c -> c.code in 33..126 } }
                ?: return failure("chitraagni_pairing_required")
            val url = endpoint.origin.toString().toHttpUrl().newBuilder().apply {
                if (endpoint.route == WorkerRoute.TAILSCALE) addPathSegment("megas")
                listOf("api", "local", "v1", "workers", endpoint.workerId, "chitraagni", "tools")
                    .forEach(::addPathSegment)
            }.build()
            val body = arguments.put("owner_sha256", digest(selected.ownerUid)).toString()
            val request = Request.Builder().url(url)
                .header("Accept", "application/json")
                .header("X-Megas-Protocol", "1")
                .header("Authorization", "Bearer $credential")
                .post(body.toRequestBody("application/json".toMediaType())).build()
            if (!selected.isCurrent()) return failure("chitraagni_selection_changed")
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return failure("gateway_request_failed")
                val source = response.body?.source() ?: return failure("invalid_gateway_response")
                if (source.request(MAX_RESPONSE_BYTES + 1)) return failure("response_too_large")
                val page = JSONObject(source.readUtf8())
                val tools = page.getJSONArray("tools")
                if (tools.length() > arguments.optInt("limit", 5)) return failure("invalid_gateway_response")
                val capabilities = JSONArray()
                for (i in 0 until tools.length()) {
                    val row = tools.getJSONObject(i)
                    val name = row.get("name") as? String ?: return failure("invalid_gateway_response")
                    val description = row.get("description") as? String ?: return failure("invalid_gateway_response")
                    if (!TOOL_NAME.matches(name)) return failure("invalid_gateway_response")
                    capabilities.put(JSONObject().put("name", name).put("description", description)
                        .put("inputSchema", row.getJSONObject("inputSchema")))
                }
                val next = page.get("next_offset")
                if (next !== JSONObject.NULL && (next !is Int || tools.length() == 0 || next != arguments.optInt("offset", 0) + tools.length())) {
                    return failure("invalid_gateway_response")
                }
                if (!selected.isCurrent()) return failure("chitraagni_selection_changed")
                JSONObject().put("tools", capabilities).put("next_offset", next).put("ok", true).toString()
            }
        } catch (_: Exception) {
            failure("android_operation_unavailable")
        }
    }

    private fun failure(code: String): String =
        JSONObject().put("ok", false).put("error", JSONObject().put("code", code)).toString()

    private companion object {
        const val MAX_RESPONSE_BYTES = 60L * 1024
        val TOOL_NAME = Regex("chitraagni_[a-z0-9_]{1,117}")
    }
}
