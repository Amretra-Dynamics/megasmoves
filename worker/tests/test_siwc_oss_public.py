"""Offline integration tests for public MIT MegasMoves Sign in with ChatGPT."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen
from urllib.error import HTTPError

import httpx

from megas.siwc_oss import (
    AUTH, API, OSSChatGPTClient, OSSRecords, ChatGPTConnectionError,
    _verify_signed_identity, LinuxSecretToolVault,
)

ACCOUNT = "owner@example.test"
ISSUED = "oaiapp_megasossfixture1234"


class MemoryVault:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def get_password(self, service, key):
        with self.lock:
            return self.items.get((service, key))

    def set_password(self, service, key, value):
        with self.lock:
            self.items[(service, key)] = value

    def delete_password(self, service, key):
        with self.lock:
            self.items.pop((service, key), None)


class PublicClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="megas-siwc-test-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.vault = MemoryVault()
        self.records = OSSRecords(self.home, vault=self.vault)
        self.calls = []
        self.mode = "success"
        self.refresh_calls = 0

        def handler(req):
            self.calls.append(req)
            path = req.url.path
            if path == "/api/accounts/oauth/token":
                fields = dict(p.split("=", 1) for p in req.content.decode().split("&"))
                if fields.get("grant_type") == "refresh_token":
                    self.refresh_calls += 1
                    time.sleep(.08)
                    return httpx.Response(200, json={
                        "access_token": "access-rotated",
                        "refresh_token": "refresh-rotated", "expires_in": 3600,
                        "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
                    })
                if self.mode == "invalid_grant":
                    return httpx.Response(400, json={"error": "invalid_grant"})
                scope = ("openid email" if self.mode == "identity_only" else
                         "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct")
                return httpx.Response(200, json={
                    "id_token": "fake-signed-token", "access_token": "access-registered",
                    "refresh_token": "refresh-registered", "expires_in": 3600,
                    "scope": scope,
                })
            if path == "/v1/models":
                return httpx.Response(200, json={"models": [
                    {"slug": "gpt-6.2-sol", "display_name": "Test model", "visibility": "list"},
                    {"slug": "hidden-model", "visibility": "hidden"},
                ]})
            if path == "/v1/responses":
                if self.mode == "usage_limit":
                    stream = ('data: {"type":"response.failed","response":{"error":'
                              '{"code":"subscription_sharing_usage_limit_exceeded"}}}\n\n')
                else:
                    stream = ('data: {"type":"response.output_text.delta","delta":"Ready"}\n\n'
                              'data: {"type":"response.completed"}\n\n'
                              'data: [DONE]\n\n')
                return httpx.Response(200, text=stream)
            if path == "/.well-known/openid-configuration":
                return httpx.Response(200, json={"revocation_endpoint": AUTH + "/revoke"})
            if path == "/revoke":
                return httpx.Response(200)
            return httpx.Response(404)

        self.http = httpx.Client(transport=httpx.MockTransport(handler))
        self.client = OSSChatGPTClient(records=self.records, http=self.http)
        self.urls = []
        from megas import siwc_oss
        self.real_chrome_profile = siwc_oss.chrome_profile
        self.open_profile = patch(
            "megas.siwc_oss.chrome_profile",
            return_value=(self.home, "/usr/bin/google-chrome", "Default"),
        )
        self.launch_profile = patch(
            "megas.siwc_oss._launch_existing_profile",
            side_effect=lambda url, profile: (self.urls.append(url) or "Default"),
        )
        self.signed_identity = patch(
            "megas.siwc_oss._verify_signed_identity",
            side_effect=lambda jwt, client, nonce, http: {
                "issuer": AUTH, "subject": "subject-fixture",
                "email": ("other@example.test" if self.mode == "wrong_account" else ACCOUNT),
            },
        )
        for context in (self.open_profile, self.launch_profile, self.signed_identity):
            self.addCleanup(context.stop)
            context.start()

    def start_and_callback(self, *, error=None, expected_client=None):
        result = self.client.begin(chrome_profile_name="Existing Chrome", account_email=ACCOUNT)
        self.assertEqual(result["state"], "waiting_for_browser")
        self.assertEqual(result["profile_id"], "Default")
        query = parse_qs(urlsplit(self.urls[-1]).query)
        if expected_client:
            self.assertEqual(query["client_id"], [expected_client])
        self.assertTrue(query["ext_agent_host_id"][0].startswith("urn:uuid:"))
        self.assertEqual(query["code_challenge_method"], ["S256"])
        values = {"state": query["state"][0]}
        if error:
            values["error"] = error
        else:
            values.update(code="test-oauth-code", client_id=ISSUED)
        with urlopen(query["redirect_uri"][0] + "?" + urlencode(values), timeout=3) as resp:
            self.assertEqual(resp.status, 200)
        return self.client.wait(timeout=5)


    def test_first_time_grant_models_inference_and_logout(self):
        outcome = self.start_and_callback(expected_client="dynamic_agent_client")
        self.assertTrue(outcome["plan_enabled"])
        self.assertTrue(outcome["connected"])
        self.assertEqual(outcome["email"], ACCOUNT)
        self.assertEqual(self.records.selected(ACCOUNT)["client_id"], ISSUED)
        self.assertEqual(self.records.issued_for(ACCOUNT), "")
        self.assertNotIn("access-registered", self.records.index_file.read_text())
        self.assertEqual(self.records.index_file.stat().st_mode & 0o077, 0)
        models = self.client.models(email=ACCOUNT)
        self.assertEqual(models, [{"slug": "gpt-6.2-sol",
                                   "display_name": "Test model"}])
        result = self.client.infer(email=ACCOUNT, model="gpt-6.2-sol",
                                   prompt="Make an agent coordination plan")
        self.assertEqual(result["text"], "Ready")
        self.assertEqual(result["provider"], "chatgpt_plan")
        request = next(r for r in self.calls if r.url.path == "/v1/responses")
        body = json.loads(request.content)
        self.assertFalse(body["store"])
        self.assertTrue(body["stream"])
        self.assertNotIn("multi_agent", body)
        self.assertNotIn("background", body)
        self.assertTrue(request.headers["authorization"].startswith("Bearer access-"))
        disconnected = self.client.disconnect(email=ACCOUNT)
        self.assertTrue(disconnected["disconnected"])
        self.assertTrue(disconnected["remote_revocation_confirmed"])
        self.assertFalse(self.client.status(ACCOUNT)["plan_enabled"])
        self.assertEqual(self.records.selected(ACCOUNT)["client_id"], ISSUED)
        self.assertEqual(self.records.get(self.records.selected(ACCOUNT))["access_token"], "")
        self.start_and_callback(error="access_denied", expected_client=ISSUED)
        self.assertEqual(self.client.status()["state"], "declined")

    def test_invalid_grant_preserves_issued_client(self):
        self.mode = "invalid_grant"
        status = self.start_and_callback(expected_client="dynamic_agent_client")
        self.assertEqual(status["state"], "failed")
        self.assertFalse(status["connected"])
        self.assertEqual(self.records.issued_for(ACCOUNT), ISSUED)
        self.assertEqual(len(self.vault.items), 0)
        self.start_and_callback(error="access_denied", expected_client=ISSUED)

    def test_wrong_account_never_saves_credentials(self):
        self.mode = "wrong_account"
        status = self.start_and_callback()
        self.assertFalse(status["connected"])
        self.assertEqual(status["state"], "failed")
        self.assertFalse(self.vault.items)
        self.assertEqual(self.records.issued_for(ACCOUNT), ISSUED)

    def test_identity_only_cannot_use_plan(self):
        self.mode = "identity_only"
        status = self.start_and_callback()
        self.assertTrue(status["connected"])
        self.assertFalse(status["plan_enabled"])
        with self.assertRaisesRegex(ChatGPTConnectionError, "permission was not granted"):
            self.client.models(email=ACCOUNT)
        self.assertFalse(any(r.url.path == "/v1/models" for r in self.calls))

    def test_callback_rejects_wrong_state(self):
        result = self.client.begin(chrome_profile_name="Existing Chrome",
                                   account_email=ACCOUNT)
        self.assertEqual(result["state"], "waiting_for_browser")
        params = parse_qs(urlsplit(self.urls[-1]).query)
        uri = params["redirect_uri"][0]
        with self.assertRaises(HTTPError) as error:
            urlopen(uri + "?state=wrong&code=forged&client_id=" + ISSUED, timeout=3)
        self.assertEqual(error.exception.code, 400)
        self.assertFalse(self.vault.items)
        with urlopen(uri + "?" + urlencode({
            "state": params["state"][0], "error": "access_denied",
        }), timeout=3) as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(self.client.wait(timeout=5)["state"], "declined")

    def test_usage_limit_never_invokes_paid_fallback(self):
        self.start_and_callback()
        self.mode = "usage_limit"
        with self.assertRaisesRegex(ChatGPTConnectionError, "no paid fallback"):
            self.client.infer(email=ACCOUNT, model="gpt-6.2-sol",
                              prompt="Perform a small inference")
        count = len([r for r in self.calls if r.url.path == "/v1/responses"])
        self.assertEqual(count, 1)


    def test_rotating_refresh_is_locked_between_two_local_clients(self):
        self.start_and_callback()
        entry = self.records.selected(ACCOUNT)
        saved = self.records.get(entry)
        saved["expires_at"] = time.time() - 10
        self.records.save(saved)
        a = OSSChatGPTClient(records=OSSRecords(self.home, vault=self.vault), http=self.http)
        b = OSSChatGPTClient(records=OSSRecords(self.home, vault=self.vault), http=self.http)
        barrier = threading.Barrier(2)
        def refresh(client):
            barrier.wait(timeout=3)
            return client._access_token(ACCOUNT)
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(refresh, a)
            two = pool.submit(refresh, b)
            self.assertEqual(one.result(timeout=6), "access-rotated")
            self.assertEqual(two.result(timeout=6), "access-rotated")
        self.assertEqual(self.refresh_calls, 1)
        self.assertEqual(self.records.get(entry)["refresh_token"], "refresh-rotated")
        self.assertFalse(any("refresh-rotated" in p.read_text(errors="replace")
                             for p in self.home.glob("*.json")))

    def test_secure_linux_secret_service_does_not_put_password_in_argv(self):
        from unittest.mock import MagicMock
        vault = LinuxSecretToolVault()
        observed = []
        def fake_run(argv, **kw):
            observed.append((argv, kw))
            return MagicMock(returncode=0, stdout="secret-record\n", stderr="")
        with patch("megas.siwc_oss.subprocess.run", side_effect=fake_run):
            ref = "a" * 64
            vault.set_password(OSSRecords.SERVICE, ref, "private-test-token")
            self.assertEqual(vault.get_password(OSSRecords.SERVICE, ref),
                             "secret-record")
            vault.delete_password(OSSRecords.SERVICE, ref)
        self.assertEqual(len(observed), 3)
        self.assertIn("private-test-token", observed[0][1]["input"])
        self.assertTrue(all("private-test-token" not in str(argv)
                            for argv, _ in observed))

    def test_untrusted_plaintext_keyring_backend_is_not_accepted(self):
        import types
        import sys
        class InsecureBackend:
            priority = 100
        InsecureBackend.__module__ = "keyrings.alt.file"
        fake_keyring = types.SimpleNamespace(get_keyring=lambda: InsecureBackend())
        with patch.dict(sys.modules, {"keyring": fake_keyring}), \
             patch("megas.siwc_oss.shutil.which", return_value="/usr/bin/secret-tool"):
            # Even with a high priority, a plaintext keyring is rejected in
            # favor of the native Linux Secret Service.
            self.assertIsInstance(OSSRecords(self.home).vault, LinuxSecretToolVault)

    def test_existing_chrome_profile_selection_has_no_default_fallback(self):
        chrome_profile = self.real_chrome_profile
        root = self.home / ".config" / "google-chrome"
        root.mkdir(parents=True)
        (root / "Default").mkdir()
        (root / "Profile 2").mkdir()
        (root / "Local State").write_text(json.dumps({
            "profile": {"info_cache": {
                "Default": {"name": "Existing Chrome"},
                "Profile 2": {"name": "Other Chrome"},
            }}
        }))
        executable = self.home / "chrome-stub"
        executable.write_text("stub")
        with patch("megas.siwc_oss.Path.home", return_value=self.home), \
             patch("megas.siwc_oss.shutil.which", return_value=str(executable)):
            self.assertEqual(chrome_profile("Existing Chrome")[2], "Default")
            with self.assertRaises(ChatGPTConnectionError):
                chrome_profile("nonexistent-profile")
