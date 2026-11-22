# Sign in with ChatGPT: local open-source client

**Status:** local open-source integration preview. Not a remotely hosted
or commercial subscription-sharing service. The client runs on your own
machine and uses official OpenAI authorization code + PKCE.

Source: [MegasMoves (MIT)](https://github.com/Amretra-Dynamics/megasmoves).
Specification: [OpenAI Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in).

## Install

Use Python 3.11+ and a supported OS credential vault. Install:

~~~sh
python -m pip install -r worker/requirements-siwc-oss.txt
~~~

On Linux, the current user's Secret Service (through `secret-tool`) can
store credentials if no Python keyring backend is available. Windows and
macOS use the OS-backed `keyring` service. There is no plaintext-token
fallback.

## Commands

Choose the **existing Chrome profile name**, not the ChatGPT account name.
For example, a Chrome profile named `Your Chrome` can already be signed into
a ChatGPT email address:

~~~sh
PYTHONPATH=worker python -m megas.siwc_oss status

PYTHONPATH=worker python -m megas.siwc_oss sign-in \
  --profile "Your Chrome" --account "you@example.com"

PYTHONPATH=worker python -m megas.siwc_oss models \
  --account "you@example.com"

PYTHONPATH=worker python -m megas.siwc_oss ask \
  --account "you@example.com" --model "MODEL_FROM_LIST"

PYTHONPATH=worker python -m megas.siwc_oss disconnect \
  --account "you@example.com"
~~~

For `ask`, type the prompt on standard input and finish with EOF. Do not
put credentials or private prompts in process command-line arguments.

## Security and eligibility

On first registration, Megas uses `dynamic_agent_client` and
`agent_name_hint=MegasMoves`, plus a stable host ID, PKCE, nonce and state.
Only an existing Chrome profile is opened. The HTTP loopback callback binds
to `127.0.0.1` and validates the state; the issued `oaiapp_...` client
ID is retained for the same ChatGPT account and host.

ID tokens are verified against OpenAI's JWKS (RS256), issuer, audience,
expiry and nonce. The signed email must match the selected account.
Session tokens are stored in the local OS credential vault, not the
repository or local metadata files. Refresh-token rotation is protected
by a cross-process lock.

Model listing and inference require the explicit
`chatgpt.tokens.use.direct` scope. The model catalog uses
`GET https://api.openai.com/v1/models`. The Responses API requests use
`store=false`, `stream=true` and succeed only after
`response.completed`. No Codex credentials, ChatGPT browser cookies,
hosted multi-agent parameters, backend-api routes, or paid API fallback.

Local OSS publication does not itself confirm plan eligibility or an issued
OAuth grant. An upstream consent timeout cannot be bypassed or treated as
success. Sign-in and model access depend on OpenAI's provider response and
the account's eligible plan and deployment context.

## Verify the source

~~~sh
PYTHONPATH=worker python -m unittest discover -s worker/tests -p 'test_siwc_oss_public.py' -v
PYTHONPATH=worker python -m unittest discover -s worker/tests -p 'test_siwc_oss_crypto.py' -v
~~~

These tests use synthetic provider responses. Claim a live connection only
after a real browser callback, signed identity, granted scope, account-specific
model listing and completed Responses stream. The installed native
MegasMoves executable requires separate release smoke tests.
