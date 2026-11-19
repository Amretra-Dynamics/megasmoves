package com.amretra.megasmoves.domain

import kotlinx.serialization.Serializable

@Serializable
data class SendInstructionResult(
    val success: Boolean,
    val sessionId: String? = null,
    /** Parent turn when a follow-up creates a linked child session. */
    val parentSessionId: String? = null,
    /** False for legacy backends that continue in the original session. */
    val createdNewSession: Boolean = false,
    val taskId: String? = null,
    val status: String? = null,
    val error: String? = null,
    val humanPlan: String? = null,
    val headline: String? = null,
    val preflightNotes: List<String> = emptyList(),
    val continuedFromSessionId: String? = null,
)
