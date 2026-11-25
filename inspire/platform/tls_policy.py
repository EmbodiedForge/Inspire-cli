"""TLS verification policy for the channels that carry the platform session cookie.

Ground truth, measured 2026-10-01 with `openssl s_client`: `qz.sii.edu.cn`, `cas.sii.edu.cn`,
`keycloak-inspire-prod.sii.edu.cn` and `docker-qb.sii.edu.cn` all serve the same publicly trusted
DigiCert chain for `*.sii.edu.cn` and verify against the system store with return code 0. There is
therefore no platform-side reason to skip verification, and #65 is about three code paths that did
it anyway on sockets carrying the CAS session cookie.

Verification is the default on every channel. `api.skip_ssl_verify` / ``INSPIRE_SKIP_SSL_VERIFY``
is the single opt-out, for a host whose chain really is broken (a TLS-intercepting proxy, an
off-network mirror); taking it is loud - the first use of each channel prints one warning to
stderr, and urllib3's ``InsecureRequestWarning`` is silenced only while the opt-out is in effect.
"""

from __future__ import annotations

import os
import ssl
import sys
import urllib3

ENV_VAR = "INSPIRE_SKIP_SSL_VERIFY"

_TRUE_VALUES = ("1", "true", "yes", "on")

_warned_channels: set[str] = set()
_warnings_suppressed = False


def skip_verification() -> bool:
    """True when the operator opted out of certificate verification for this run.

    Env wins over the file so a one-off `INSPIRE_SKIP_SSL_VERIFY=1` works without editing config.
    A config that cannot be read is not read as consent: the answer stays False.
    """
    if os.environ.get(ENV_VAR, "").strip().lower() in _TRUE_VALUES:
        return True
    try:
        from inspire.config import Config

        config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
    except Exception:  # noqa: BLE001 - unreadable config means no opt-out, not an error here
        return False
    return bool(getattr(config, "skip_ssl_verify", False))


def verify_setting() -> bool:
    """Value for ``requests.Session.verify``."""
    return not skip_verification()


def ignore_https_errors() -> bool:
    """Value for a Playwright browser context's ``ignore_https_errors``."""
    if not skip_verification():
        return False
    warn_insecure_channel("browser automation")
    return True


def ssl_options() -> dict[str, int]:
    """``sslopt`` for websocket-client.

    websocket-client >= 1.9 defaults to ``ssl.CERT_REQUIRED`` when `sslopt` is empty (measured on
    1.9.2: an empty `sslopt` rejects a self-signed certificate), so verifying is the empty case.
    It is spelled out anyway because older releases of that library defaulted to ``CERT_NONE``, and
    ``CERT_NONE`` should only ever appear here, on the opt-out path, next to the warning saying so.
    """
    if not skip_verification():
        return {"cert_reqs": ssl.CERT_REQUIRED}
    warn_insecure_channel("exec WebSocket")
    return {"cert_reqs": ssl.CERT_NONE}


def suppress_insecure_warnings() -> None:
    """Silence urllib3's per-request insecure warning - and only ever from the opt-out path.

    Used to run at import of `inspire.platform.openapi.client`, which hid the warning even when
    verification was on. The single stderr banner from `warn_insecure_channel` is the signal; this
    only stops the same fact repeating once per request underneath it.
    """
    global _warnings_suppressed
    if _warnings_suppressed:
        return
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    _warnings_suppressed = True


def warn_insecure_channel(channel: str) -> None:
    """Say once per run, on stderr, that `channel` is not verifying certificates."""
    if channel in _warned_channels:
        return
    _warned_channels.add(channel)
    suppress_insecure_warnings()
    print(
        f"inspire: WARNING: TLS certificate verification is DISABLED for {channel} "
        f"(api.skip_ssl_verify / {ENV_VAR}). Session cookies travel unverified on this channel; "
        "anyone who can intercept the connection holds them. Only set this for a host whose "
        "certificate chain is genuinely broken.",
        file=sys.stderr,
    )
