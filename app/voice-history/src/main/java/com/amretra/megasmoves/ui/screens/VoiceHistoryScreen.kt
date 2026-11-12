package com.amretra.megasmoves.ui.screens

import android.app.Activity
import android.content.Intent
import android.speech.RecognizerIntent
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.key
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.amretra.megasmoves.core.auth.VerifiedOwnerLifetime
import com.amretra.megasmoves.domain.SendInstructionResult
import com.amretra.megasmoves.core.voice.VoiceEntry
import com.amretra.megasmoves.core.voice.VoiceHistory
import com.amretra.megasmoves.core.voice.VoicePlanTarget
import java.text.DateFormat
import java.util.Date
import java.util.UUID
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * The host supplies its verified login and the agents visible to that user.
 * [sendPlan] must use the pending entry ID as its idempotency key, route to the
 * saved target, and validate the supplied login before sending the transcript.
 */
@Composable
fun VoiceHistoryScreen(
    owner: VerifiedOwnerLifetime?,
    targets: List<VoicePlanTarget>,
    onBack: () -> Unit,
    onCall: () -> Unit,
    onOpenAgent: (String) -> Unit,
    sendPlan: suspend (VoiceEntry, VerifiedOwnerLifetime) -> SendInstructionResult,
) {
    val context = LocalContext.current
    key(owner) {
        val history = remember(owner) { owner?.let { VoiceHistory.open(context, it) } }
        VoiceHistoryContent(owner, history, targets, onBack, onCall, onOpenAgent) { entry ->
            check(owner?.isCurrent() == true) { "Sign in again to send a voice note." }
            sendPlan(entry, requireNotNull(owner))
        }
    }
}

@Composable
internal fun VoiceHistoryContent(
    owner: VerifiedOwnerLifetime?,
    history: VoiceHistory?,
    targets: List<VoicePlanTarget>,
    onBack: () -> Unit,
    onCall: () -> Unit,
    onOpenAgent: (String) -> Unit,
    sendPlan: suspend (VoiceEntry) -> SendInstructionResult,
) {
    val revision by VoiceHistory.changes.collectAsState()
    var rows by remember { mutableStateOf(emptyList<VoiceEntry>()) }
    var unreadable by remember { mutableStateOf(0) }
    var selectedId by rememberSaveable { mutableStateOf<String?>(null) }
    var dictationId by rememberSaveable { mutableStateOf<String?>(null) }
    var message by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    var deleting by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val selected = rows.firstOrNull { it.id == selectedId }
    BackHandler(enabled = selectedId != null) { selectedId = null; message = null }
    LaunchedEffect(history, revision) {
        if (history != null) {
            runCatching { withContext(Dispatchers.IO) { history.readHistory() } }
                .onSuccess { rows = it.entries; unreadable = it.unreadable }
                .onFailure { rows = emptyList(); unreadable = 0; message = it.message }
        }
    }
    val dictation = rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val sourceId = dictationId
        dictationId = null
        val alternatives = result.data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
        val completed = result.resultCode == Activity.RESULT_OK
        if (history != null && owner?.isCurrent() == true) {
            scope.launch {
                runCatching { withContext(Dispatchers.IO) { history.captureDictation(sourceId, completed, alternatives) } }
                    .onSuccess { entry ->
                        if (entry != null) selectedId = entry.id
                        else if (completed) message = "No speech was recognized."
                    }
                    .onFailure { message = it.message }
            }
        }
    }

    fun send(entry: VoiceEntry, target: VoicePlanTarget) {
        if (owner == null || history == null || busy) return
        busy = true
        message = null
        scope.launch {
            try {
                val pending = withContext(Dispatchers.IO) { history.beginPlan(entry.id, target) }
                if (pending.sessionId == null) {
                    val result = sendPlan(pending)
                    val session = result.sessionId
                    check(result.success && !session.isNullOrBlank()) { result.error ?: "Agent did not accept the note." }
                    withContext(Dispatchers.IO) { history.accepted(entry.id, session, result.taskId) }
                }
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (e: Exception) {
                message = e.message ?: "Could not contact agent. Retry with the same note."
            } finally {
                busy = false
            }
        }
    }

    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            TextButton(onClick = { if (selectedId == null) onBack() else { selectedId = null; message = null } }) { Text("Back") }
            Text("Voice history", style = MaterialTheme.typography.headlineSmall, modifier = Modifier.padding(top = 8.dp))
        }
        if (owner?.isCurrent() != true || history == null) {
            Text("Sign in to open your private voice history.")
            return@Column
        }
        message?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        if (unreadable > 0) Text("Could not open $unreadable saved voice note(s).", color = MaterialTheme.colorScheme.error)
        LazyColumn(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            if (selected == null) {
                item {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(onClick = {
                            val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                                putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                                putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
                            }
                            dictationId = UUID.randomUUID().toString()
                            runCatching { dictation.launch(intent) }.onFailure {
                                dictationId = null
                                message = "Device dictation is unavailable. Use Live voice to save a spoken turn."
                            }
                        }) { Text("Dictate a note") }
                        OutlinedButton(onClick = onCall) { Text("Live voice") }
                    }
                }
                item { Text("Dictated notes and your spoken turns in Live voice are saved on this device. Choose a note to send it to an agent.") }
                if (rows.isEmpty() && unreadable == 0) item { Text("No saved voice notes yet.") }
                items(rows, key = { it.id }) { entry ->
                    Card(Modifier.fillMaxWidth().clickable { selectedId = entry.id; message = null }) {
                        Column(Modifier.padding(14.dp)) {
                            Text(DateFormat.getDateTimeInstance().format(Date(entry.createdAt)), style = MaterialTheme.typography.labelMedium)
                            Text(entry.transcript, maxLines = 3, overflow = TextOverflow.Ellipsis)
                        }
                    }
                }
            } else {
                item { Text(DateFormat.getDateTimeInstance().format(Date(selected.createdAt)), style = MaterialTheme.typography.labelMedium) }
                item { Text(selected.transcript) }
                if (selected.sessionId != null && selected.target != null) {
                    item {
                        Text("Sent to ${selected.target.label}. Open the agent conversation for the saved Action and Tasks.")
                        TextButton(onClick = { onOpenAgent(selected.target.agentId) }) { Text("Open agent conversation") }
                    }
                } else {
                    item { Text("Send this transcript to an agent to plan an Action and Tasks.", style = MaterialTheme.typography.titleMedium) }
                    if (selected.target != null) {
                        item { Button(enabled = !busy, onClick = { send(selected, selected.target) }) { Text("Retry with ${selected.target.label}") } }
                    } else {
                        if (targets.isEmpty()) item { Text("Connect an agent in Systems to plan this note.") }
                        items(targets, key = { it.agentId }) { target ->
                            OutlinedButton(enabled = !busy, onClick = { send(selected, target) }) { Text(target.label) }
                        }
                    }
                }
                item { TextButton(enabled = !busy, onClick = { deleting = true }) { Text("Delete note") } }
            }
            item { Spacer(Modifier.height(32.dp)) }
        }
    }
    if (deleting && selected != null && history != null) {
        AlertDialog(
            onDismissRequest = { deleting = false },
            title = { Text("Delete this voice note?") },
            text = { Text("This removes the transcript from this device.") },
            confirmButton = {
                TextButton(onClick = {
                    deleting = false
                    scope.launch {
                        runCatching { withContext(Dispatchers.IO) { history.delete(selected.id) } }
                            .onSuccess { selectedId = null }
                            .onFailure { message = it.message }
                    }
                }) { Text("Delete") }
            },
            dismissButton = { TextButton(onClick = { deleting = false }) { Text("Cancel") } },
        )
    }
}
