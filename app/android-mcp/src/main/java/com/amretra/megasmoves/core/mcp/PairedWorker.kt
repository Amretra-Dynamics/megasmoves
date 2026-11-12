package com.amretra.megasmoves.core.mcp

import java.net.URI
import java.security.MessageDigest

enum class WorkerRoute { DIRECT, TAILSCALE, TUNNEL }

data class WorkerEndpoint(
    val endpointId: String,
    val workerId: String,
    val installationId: String,
    val origin: URI,
    val route: WorkerRoute,
    val verifiedAt: String?,
)

data class WorkerPairing(
    val isPaired: Boolean,
    val workerId: String,
    val installationId: String,
    val deviceId: String,
    val ownerUid: String,
    val scopes: Set<String>,
)

/** An immutable selection from the host's verified login and private pairing store. */
class SelectedWorker(
    val ownerUid: String,
    val endpoint: WorkerEndpoint,
    val pairing: WorkerPairing,
    val selectedKey: String,
    /** Return false when the login lifetime, worker selection or credential changes. */
    val isCurrent: () -> Boolean,
    /** Read from the current secure credential store only when authorized. */
    val readCredential: () -> String?,
)

/** Calculate the selection key for a worker the user has verified and paired in Megas. */
fun workerSelectionKey(ownerUid: String, endpoint: WorkerEndpoint, pairing: WorkerPairing): String? {
    val origin = endpoint.origin
    val host = origin.host ?: return null
    if (!origin.scheme.equals("https", true) || origin.rawUserInfo != null ||
        origin.rawQuery != null || origin.rawFragment != null || origin.rawPath.orEmpty() !in setOf("", "/") ||
        (origin.port != -1 && origin.port !in 1..65535) ||
        (endpoint.route == WorkerRoute.TAILSCALE && !host.endsWith(".ts.net", ignoreCase = true))) return null
    if (ownerUid.isBlank() || pairing.ownerUid != ownerUid || !pairing.isPaired ||
        endpoint.verifiedAt.isNullOrBlank() || endpoint.installationId != pairing.installationId ||
        endpoint.workerId != pairing.workerId || !WORKER_ID.matches(endpoint.workerId) ||
        "chitraagni:read" !in pairing.scopes) return null
    val port = if (origin.port == -1) 443 else origin.port
    val identity = listOf(endpoint.endpointId, endpoint.workerId, "https://$host:$port", endpoint.route.name,
        pairing.installationId, pairing.deviceId, pairing.scopes.sorted().joinToString(","))
    if (identity.any { it.isBlank() || '\u0000' in it } ||
        pairing.scopes.any { it.isBlank() || ',' in it || '\u0000' in it }) return null
    return digest(identity.joinToString("\u0000"))
}

private val WORKER_ID = Regex("[A-Za-z0-9_][A-Za-z0-9_-]{0,239}")

internal fun digest(value: String): String = MessageDigest.getInstance("SHA-256")
    .digest(value.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
