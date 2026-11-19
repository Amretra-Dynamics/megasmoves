package com.amretra.megasmoves.core.auth

import org.junit.Assert.*
import org.junit.Test

class OwnerLifetimeTest {
    @Test fun sameOwnerTokenRefreshPreservesNativeAuthority() {
        val lifetime = OwnerLifetime()
        lifetime.bind("owner")
        val captured = lifetime.capture("owner")!!
        repeat(10) { lifetime.bind("owner") }
        assertTrue(captured.isCurrent())
    }
    @Test fun logoutAndSameOwnerReloginNeverRevivesOldAuthority() {
        val lifetime = OwnerLifetime()
        lifetime.bind("owner")
        val old = lifetime.capture("owner")!!
        lifetime.bind(null)
        assertFalse(old.isCurrent())
        lifetime.bind("owner")
        assertFalse(old.isCurrent())
        assertTrue(lifetime.capture("owner")!!.isCurrent())
    }
    @Test fun accountSwitchRevokesAndCannotCaptureAnotherOwner() {
        val lifetime = OwnerLifetime()
        assertNull(lifetime.capture("owner"))
        lifetime.bind("owner")
        val old = lifetime.capture("owner")!!
        lifetime.bind("other")
        assertFalse(old.isCurrent())
        assertNull(lifetime.capture("owner"))
        assertTrue(lifetime.capture("other")!!.isCurrent())
    }
}
