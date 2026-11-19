package com.amretra.megasmoves.ui.screens

import androidx.activity.ComponentActivity
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import com.amretra.megasmoves.core.auth.VerifiedOwnerLifetime
import com.amretra.megasmoves.core.voice.VoiceHistory
import com.amretra.megasmoves.core.voice.VoicePlanTarget
import com.amretra.megasmoves.domain.SendInstructionResult
import java.util.UUID
import java.util.concurrent.atomic.AtomicInteger
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test

class VoiceHistoryScreenTest {
    @get:Rule val ui = createAndroidComposeRule<ComponentActivity>()

    @Test fun openEncryptedNoteSendAndReopenReceipt() {
        val owner = VerifiedOwnerLifetime("voice-test-${UUID.randomUUID()}") { true }
        val history = VoiceHistory.open(ui.activity, owner)
        val entry = history.saveTranscript("synthetic/test-turn", "Review the tablet layout")!!
        val target = VoicePlanTarget("agent_codex_cli__wrk_test", "wrk_test", "Codex on test PC")
        val sends = AtomicInteger()
        try {
            ui.setContent {
                VoiceHistoryContent(owner, history, listOf(target), {}, {}, {}) { pending ->
                    assertEquals(entry.id, pending.id)
                    assertEquals(target, pending.target)
                    sends.incrementAndGet()
                    SendInstructionResult(true, sessionId = "test-session", taskId = "test-task")
                }
            }
            ui.waitUntil(10_000) {
                ui.onAllNodes(androidx.compose.ui.test.hasText(entry.transcript)).fetchSemanticsNodes().isNotEmpty()
            }
            ui.onNodeWithText(entry.transcript).performClick()
            ui.onNodeWithText(target.label).performClick()
            ui.waitUntil(10_000) { history.readHistory().entries.single().sessionId != null }
            ui.onNodeWithText("Back").performClick()
            ui.onNodeWithText(entry.transcript).performClick()
            ui.onNodeWithText("Open agent conversation").assertExists()
            assertEquals("test-task", VoiceHistory.open(ui.activity, owner).readHistory().entries.single().taskId)
            assertEquals(1, sends.get())
        } finally {
            history.delete(entry.id)
        }
    }
}
