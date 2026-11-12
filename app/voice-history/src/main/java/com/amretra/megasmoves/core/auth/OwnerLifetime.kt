package com.amretra.megasmoves.core.auth

/**
 * Call [bind] on the verified account's sign-in, sign-out and account-change events.
 * A refreshed token for the same user keeps existing operations bound to their login.
 */
class OwnerLifetime {
    private class Epoch(val uid: String)
    @Volatile private var active: Epoch? = null

    @Synchronized fun bind(uid: String?) {
        if (uid.isNullOrBlank()) active = null
        else if (active?.uid != uid) active = Epoch(uid)
    }

    fun capture(uid: String): VerifiedOwnerLifetime? {
        val epoch = active?.takeIf { it.uid == uid } ?: return null
        return VerifiedOwnerLifetime(uid) { active === epoch }
    }
}

class VerifiedOwnerLifetime internal constructor(val uid: String, private val current: () -> Boolean) {
    fun isCurrent(): Boolean = current()
    override fun toString() = "VerifiedOwnerLifetime(redacted)"
}
