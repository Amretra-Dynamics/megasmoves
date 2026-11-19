package com.amretra.megasmoves.core.mcp

import java.net.URI
import java.util.UUID
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test

class AndroidMcpDiscoveryTest {
    private val server = MockWebServer()
    private lateinit var client: OkHttpClient
    private lateinit var endpoint: WorkerEndpoint
    private val pair = WorkerPairing(true, "worker_1", "installation", "test-device", "owner", setOf("chitraagni:read"))
    private val generated = UUID.randomUUID().toString()
    @Volatile private var current = true
    private var reads = 0
    private val tool = "android_chitraagni_tools"
    private val page = """{"tools":[{"name":"chitraagni_sample","description":"Test","inputSchema":{}}],"next_offset":null}"""

    @Before fun start() {
        val certificate = HeldCertificate.Builder().commonName("localhost").addSubjectAlternativeName("localhost").build()
        val serverTls = HandshakeCertificates.Builder().heldCertificate(certificate).build()
        val clientTls = HandshakeCertificates.Builder().addTrustedCertificate(certificate.certificate).build()
        client = OkHttpClient.Builder().sslSocketFactory(clientTls.sslSocketFactory(), clientTls.trustManager).build()
        server.useHttps(serverTls.sslSocketFactory(), false)
        server.start()
        endpoint = WorkerEndpoint("endpoint", "worker_1", "installation", URI("https", null, "localhost", server.port,
            null, null, null), WorkerRoute.TUNNEL, "verified")
    }

    @After fun stop() = server.shutdown()

    private fun handler(e: WorkerEndpoint = endpoint, p: WorkerPairing = pair,
                        selected: String = requireNotNull(workerSelectionKey("owner", endpoint, pair)),
                        credential: () -> String? = { generated }): AndroidMcpDiscovery =
        AndroidMcpDiscovery({ SelectedWorker("owner", e, p, selected, { current }, { reads++; credential() }) }, client)

    @Test fun discoversOnlyThroughThePairedHttpsWorker() {
        server.enqueue(MockResponse().setBody(page))
        val response = JSONObject(handler().handle(tool, """{"offset":4,"limit":1}"""))
        assertTrue(response.getBoolean("ok"))
        assertEquals(1, response.getJSONArray("tools").length())
        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/local/v1/workers/worker_1/chitraagni/tools", request.path)
        assertEquals("Bearer $generated", request.getHeader("Authorization"))
        val sent = JSONObject(request.body.readUtf8())
        assertEquals(digest("owner"), sent.getString("owner_sha256"))
        assertEquals(4, sent.getInt("offset"))
    }

    @Test fun rejectsUnverifiedOwnerRouteScopeAndChangedSelectionBeforeReadingCredentials() {
        val original = requireNotNull(workerSelectionKey("owner", endpoint, pair))
        for (changed in listOf(pair.copy(isPaired = false), pair.copy(ownerUid = "other"),
            pair.copy(installationId = "other"), pair.copy(workerId = "worker_2"),
            pair.copy(deviceId = "other"), pair.copy(scopes = setOf("conversation:read")),
            pair.copy(scopes = pair.scopes + "chitraagni:edit"))) {
            assertFalse(JSONObject(handler(p = changed, selected = original).handle(tool, "{}")).getBoolean("ok"))
        }
        for (changed in listOf(endpoint.copy(verifiedAt = null), endpoint.copy(origin = URI("http://localhost")),
            endpoint.copy(origin = URI("https://example.org")), endpoint.copy(endpointId = "other"))) {
            assertFalse(JSONObject(handler(e = changed, selected = original).handle(tool, "{}")).getBoolean("ok"))
        }
        current = false
        assertFalse(JSONObject(handler().handle(tool, "{}")).getBoolean("ok"))
        assertEquals(0, reads)
        assertEquals(0, server.requestCount)
    }

    @Test fun deniesMutationsPaginationOverridesAndMissingCredential() {
        assertFalse(JSONObject(handler().handle("android_chitraagni_call", "{}")).getBoolean("ok"))
        for (args in listOf("""{"limit":6}""", """{"offset":-1}""", """{"limit":"1"}""",
            """{"owner_sha256":"x"}""", """{"url":"example"}""", "{} {}")) {
            assertFalse(JSONObject(handler().handle(tool, args)).getBoolean("ok"))
        }
        assertEquals(0, reads)
        assertFalse(JSONObject(handler(credential = { null }).handle(tool, "{}")).getBoolean("ok"))
        assertEquals(0, server.requestCount)
    }

    @Test fun followsNoRedirectAndRejectsLargeResponses() {
        server.enqueue(MockResponse().setResponseCode(307).setHeader("Location", server.url("/other-path")))
        assertFalse(JSONObject(handler().handle(tool, "{}")).getBoolean("ok"))
        assertEquals(1, server.requestCount)
        server.enqueue(MockResponse().setBody(" ".repeat(60 * 1024 + 1)))
        assertFalse(JSONObject(handler().handle(tool, "{}")).getBoolean("ok"))
    }

    @Test fun rejectsUnrelatedCapabilitiesAndMalformedCursor() {
        for (body in listOf(page.replace("chitraagni_sample", "unrelated"), page.replace("null", "99"))) {
            server.enqueue(MockResponse().setBody(body))
            assertFalse(JSONObject(handler().handle(tool, "{}")).getBoolean("ok"))
        }
    }

    @Test fun discardsResultWhenLoginChangesDuringRequest() {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                current = false
                return MockResponse().setBody(page)
            }
        }
        val result = JSONObject(handler().handle(tool, "{}"))
        assertEquals("chitraagni_selection_changed", result.getJSONObject("error").getString("code"))
        assertFalse(result.has("tools"))
    }
}
