package com.amretra.megasmoves.core.voice

import com.amretra.megasmoves.core.auth.VerifiedOwnerLifetime
import org.junit.Assert.*
import org.junit.Test
import org.json.JSONObject

class VoiceHistoryTest {
    private val data = object : VoiceEntryStorage {
        val values = mutableMapOf<String, String>()
        override fun keys(prefix: String) = values.keys.filter { it.startsWith(prefix) }
        override fun read(key: String) = values[key]
        override fun write(key: String, value: String): Boolean { values[key] = value; return true }
        override fun delete(key: String) = values.remove(key) != null
    }

    @Test fun finalSpeechPersistsAcrossReopenAndRetriesOnce() {
        val owner = VerifiedOwnerLifetime("a") { true }
        val history = VoiceHistory(data, owner)
        val full = "a longer spoken turn ".repeat(100)
        val entry = history.saveTranscript("call1/item7", full)!!
        assertEquals(entry, history.saveTranscript("call1/item7", full))
        val restored = VoiceHistory(data, owner)
        assertEquals(full.trim(), restored.readHistory().entries.single().transcript)
        val agent = VoicePlanTarget("agent_1", "worker_1", "My PC")
        assertEquals(restored.beginPlan(entry.id, agent), restored.beginPlan(entry.id, agent))
        assertTrue(runCatching { restored.beginPlan(entry.id, agent.copy(workerId = "worker_2")) }.isFailure)
        val accepted = restored.accepted(entry.id, "sess_1", "task_1")
        assertEquals(accepted, VoiceHistory(data, owner).readHistory().entries.single())
        assertTrue(accepted.planningRequest().contains(entry.id))
        restored.delete(entry.id)
        assertTrue(restored.readHistory().entries.isEmpty())
    }

    @Test fun onlyFinalUserSpeechAndSuccessfulDictationEnterHistory() {
        val history = VoiceHistory(data, VerifiedOwnerLifetime("owner") { true })
        fun event(type: String, text: String) = JSONObject().put("type", type)
            .put("item_id", "speech-1").put("transcript", text)

        assertNull(history.captureFinalTurn("call", event("conversation.item.input_audio_transcription.delta", "part")))
        assertNull(history.captureFinalTurn("call", event("response.output_audio_transcript.done", "assistant")))
        val complete = event("conversation.item.input_audio_transcription.completed", "first user turn")
        val first = history.captureFinalTurn("call", complete)!!
        assertEquals(first, history.captureFinalTurn("call", complete.put("event_id", "replay")))
        assertNull(history.captureDictation("dictation-1", false, listOf("cancelled")))
        assertNull(history.captureDictation(null, true, listOf("unsolicited")))
        assertNull(history.captureDictation("dictation-1", true, emptyList()))
        val alternative = listOf("dictated note", "another guess")
        val dictated = history.captureDictation("dictation-1", true, alternative)!!
        assertEquals(dictated, history.captureDictation("dictation-1", true, alternative))
        assertEquals(setOf("first user turn", "dictated note"), history.readHistory().entries.map { it.transcript }.toSet())
        assertEquals(2, history.readHistory().entries.size)
    }

    @Test fun damagedEntryLeavesOtherNotesReadable() {
        val history = VoiceHistory(data, VerifiedOwnerLifetime("owner") { true })
        val first = history.saveTranscript("a", "keep")!!
        val broken = history.saveTranscript("b", "damaged")!!
        data.values[data.values.keys.single { it.endsWith(broken.id) }] = "{"
        val snapshot = history.readHistory()
        assertEquals(listOf(first), snapshot.entries)
        assertEquals(1, snapshot.unreadable)
    }

    @Test fun accountSwitchFencesLateCallbacksAndHidesOtherOwnersNotes() {
        var current = true
        val old = VoiceHistory(data, VerifiedOwnerLifetime("a") { current })
        val first = old.saveTranscript("turn", "personal")!!
        current = false
        assertTrue(runCatching { old.saveTranscript("late", "must not save") }.isFailure)
        val other = VoiceHistory(data, VerifiedOwnerLifetime("b") { true })
        assertTrue(other.readHistory().entries.isEmpty())
        assertTrue(runCatching { other.beginPlan(first.id, VoicePlanTarget("a", "b", "pc")) }.isFailure)
        assertNotEquals(first.id, other.saveTranscript("turn", "same source id")!!.id)
    }
}
