"""A small, dependency-free RESTCONF client for IOS-XE.

Uses only ``requests`` (always present in a Nautobot environment), so this job
can be delivered via a Git Repository without baking any extra Python package
into the Nautobot web/worker images.

The client is intentionally thin: GET for operational reads and POST for YANG
RPC ``operations``. All higher-level upgrade logic lives in ``iosxe_upgrade.py``.
"""

from __future__ import annotations

import re
import time

import requests

from . import constants as C


def _redact(payload):
    """Blot out URL userinfo in a payload repr before it reaches any log.

    The character class stops only at whitespace or '@' so passwords with
    '/', quotes, or ':' still redact; '@' inside a password must be
    percent-encoded in any valid URL, so first-'@' termination is sound.
    """
    return re.sub(r"://[^@\s]+@", "://***@", str(payload))


class RestconfError(Exception):
    """Raised for any RESTCONF transport / HTTP-level failure.

    ``status_code`` carries the HTTP status when the failure was an HTTP error
    response (None for connection-level failures), so callers can distinguish a
    401 (authentication) or 403 (authorization/privilege) from connectivity.
    """

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


# IOS-XE devices almost always serve a self-signed cert; silence the noisy
# warning when verification is intentionally disabled (see constants.VERIFY_TLS).
if not C.VERIFY_TLS:
    try:  # pragma: no cover - defensive
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:  # noqa: BLE001
        pass


class RestconfClient:
    """Minimal RESTCONF client scoped to a single device."""

    def __init__(
        self,
        host,
        username,
        password,
        *,
        port=C.RESTCONF_PORT,
        verify=C.VERIFY_TLS,
        logger=None,
        log_object=None,
        debug=False,
    ):
        self.host = host
        self.port = port
        self.base_url = f"https://{host}:{port}/restconf"
        self.logger = logger
        self.log_object = log_object
        self.debug = debug

        self._session = requests.Session()
        self._session.auth = (username, password)
        self._session.verify = verify
        self._session.headers.update(
            {
                "Accept": "application/yang-data+json",
                "Content-Type": "application/yang-data+json",
            }
        )

    # -- logging helpers -----------------------------------------------------

    def _debug(self, message):
        if self.debug and self.logger is not None:
            self.logger.debug(message, extra={"object": self.log_object, "grouping": "restconf"})

    @staticmethod
    def _truncate(text, limit=2000):
        text = str(text)
        return text if len(text) <= limit else f"{text[:limit]}... [truncated]"

    # -- core requests -------------------------------------------------------

    def get(self, path, *, timeout=C.GET_TIMEOUT, ok_404=False):
        """GET a RESTCONF data resource. Returns parsed JSON (dict) or None.

        ``ok_404`` returns None instead of raising when the resource is absent
        (used to probe for optional operational data / models).
        """
        url = f"{self.base_url}/{path}"
        self._debug(f"GET {url}")
        try:
            resp = self._session.get(url, timeout=timeout)
        except requests.RequestException as exc:
            raise RestconfError(f"GET {path} failed: {exc}") from exc

        if resp.status_code == 404 and ok_404:
            self._debug(f"GET {url} -> 404 (treated as absent)")
            return None
        if not resp.ok:
            raise RestconfError(
                f"GET {path} -> HTTP {resp.status_code}: {self._truncate(resp.text)}",
                status_code=resp.status_code,
            )

        self._debug(f"GET {url} -> {resp.status_code}: {self._truncate(resp.text)}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            # A non-empty 2xx body that is not JSON is NOT device-published
            # state — coercing it to {} once let a garbage reply masquerade
            # as a genuinely empty resource and age absence-based clocks
            # (review findings, two rounds). Raise so callers treat it as an
            # unreadable poll; the empty-body {} above stays: an empty 2xx IS
            # legitimate device-published absence.
            raise RestconfError(
                f"GET {path} -> HTTP {resp.status_code} with a non-JSON body "
                f"(not device-published state): {self._truncate(resp.text)}",
                status_code=resp.status_code,
            ) from exc

    def patch(self, path, payload, *, timeout=C.GET_TIMEOUT):
        """RESTCONF plain PATCH (merge) against /restconf/data.

        Used only for the opt-in log-only logging-discriminator write — the
        job's sole running-config touch besides save-config. Merge semantics:
        listed nodes are created/updated, everything else is untouched.
        Raises RestconfError on any non-2xx or transport failure; callers
        treat suppression failures as NON-fatal (warn and continue).
        """
        url = f"{self.base_url}/{path}"
        self._debug(f"PATCH {url} body={self._truncate(_redact(payload))}")
        try:
            resp = self._session.patch(url, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            raise RestconfError(f"PATCH {path} failed: {exc}") from exc
        if not resp.ok:
            raise RestconfError(
                f"PATCH {path} -> HTTP {resp.status_code}: {self._truncate(resp.text)}",
                status_code=resp.status_code,
            )
        return True

    def post_rpc(self, operation, payload, *, timeout=C.RPC_TIMEOUT, tolerate_disconnect=False):
        """POST a YANG RPC to /restconf/operations.

        Returns parsed JSON (or {} on an empty 2xx body).

        ``tolerate_disconnect`` is for operations that reboot the device (e.g.
        ``activate``). A dropped connection returns {"_disconnected": True} ("the
        RPC was accepted and the reload has begun"); a READ TIMEOUT with the
        connection still open returns {"_timeout": True} (the engine is stuck —
        NOT a reload). HTTP 4xx/5xx and DNS/TLS/auth failures still raise.
        """
        url = f"{self.base_url}/{operation}"
        # Redact URL userinfo (ftp://user:pass@...) — copy payloads may carry
        # credentialed source URLs. Redact BEFORE truncation so a cut cannot
        # strip the trailing '@' the pattern needs (review finding).
        self._debug(f"POST {url} body={self._truncate(_redact(payload))}")
        try:
            resp = self._session.post(url, json=payload, timeout=timeout)
        except requests.exceptions.ReadTimeout as exc:
            # The connection stayed OPEN but silent: the server-side handler is
            # stuck (a real 17.15.x held an ambiguous activate for the whole
            # timeout). This is NOT a reload — a reload resets the connection.
            if tolerate_disconnect:
                self._debug(f"POST {url} timed out with the connection open: {exc}")
                return {"_timeout": True}
            raise RestconfError(f"POST {operation} failed: {exc}") from exc
        except requests.exceptions.ConnectTimeout as exc:
            # The TCP connect never completed: the request was NEVER SENT, so
            # this cannot mean 'the RPC was accepted and the reload began'
            # (review finding — ConnectTimeout subclasses ConnectionError and
            # would otherwise fall through to the disconnect arm).
            raise RestconfError(f"POST {operation} failed before sending: {exc}") from exc
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
        ) as exc:
            if tolerate_disconnect:
                self._debug(f"POST {url} disconnected (expected on reload): {exc}")
                return {"_disconnected": True}
            raise RestconfError(f"POST {operation} failed: {exc}") from exc
        except requests.RequestException as exc:
            raise RestconfError(f"POST {operation} failed: {exc}") from exc

        if not resp.ok:
            raise RestconfError(
                f"POST {operation} -> HTTP {resp.status_code}: {self._truncate(resp.text)}",
                status_code=resp.status_code,
            )

        self._debug(f"POST {url} -> {resp.status_code}: {self._truncate(resp.text)}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # -- recorder primitives ---------------------------------------------------

    @staticmethod
    def _probe_record(resp, started):
        # Decode explicitly as UTF-8 (RFC 8040/7951 mandates it for RESTCONF
        # JSON) — requests has no charset for application/yang-data+json and
        # would fall back to a chardet GUESS, silently corrupting non-ASCII in
        # an evidence capture. content_bytes is the device's true byte count.
        elapsed = int((time.monotonic() - started) * 1000)
        return {
            "status": resp.status_code,
            "elapsed_ms": elapsed,
            "text": resp.content.decode("utf-8", errors="replace"),
            "content_bytes": len(resp.content),
            "error": None,
        }

    @staticmethod
    def _probe_error(exc, started):
        elapsed = int((time.monotonic() - started) * 1000)
        return {
            "status": None,
            "elapsed_ms": elapsed,
            "text": "",
            "content_bytes": 0,
            "error": str(exc),
        }

    def probe_get(self, path, *, timeout=C.GET_TIMEOUT):
        """GET as an evidence probe: record, never judge.

        Unlike :meth:`get`, an HTTP error status is a RESULT here, not a
        failure — the RESTCONF Dev Tester job exists to capture exactly those
        responses verbatim (UTF-8-decoded per RFC 8040/7951). Returns a record
        dict ``{"status", "elapsed_ms", "text", "content_bytes", "error"}``
        where ``status`` is None only for transport-level failures (connection
        refused, timeout, TLS), with the exception text in ``error``. Never
        raises.
        """
        url = f"{self.base_url}/{path}"
        self._debug(f"PROBE GET {url}")
        started = time.monotonic()
        try:
            resp = self._session.get(url, timeout=timeout)
        except requests.RequestException as exc:
            return self._probe_error(exc, started)
        return self._probe_record(resp, started)

    def probe_post(self, operation, payload=None, *, timeout=C.RPC_TIMEOUT):
        """POST an RPC as an evidence probe: record, never judge.

        ``payload=None`` sends NO body (the RESTCONF form for no-input RPCs);
        callers may retry once with ``{}`` if the device 400s the bodiless
        form — both attempts are legitimate recorded evidence. Same record
        shape as :meth:`probe_get`; never raises.
        """
        url = f"{self.base_url}/{operation}"
        self._debug(f"PROBE POST {url} body={self._truncate(_redact(payload))}")
        started = time.monotonic()
        try:
            if payload is None:
                resp = self._session.post(url, timeout=timeout)
            else:
                resp = self._session.post(url, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            return self._probe_error(exc, started)
        return self._probe_record(resp, started)

    def clone(self):
        """A new client (fresh HTTP session) with this client's settings.

        requests.Session is not thread-safe; the copy watcher polls from its own
        session while the blocking copy RPC occupies the original one.
        """
        username, password = self._session.auth
        return RestconfClient(
            self.host,
            username,
            password,
            port=self.port,
            verify=self._session.verify,
            logger=self.logger,
            log_object=self.log_object,
            debug=self.debug,
        )

    def ping(self):
        """Reachability check used to detect when a device is back up.

        Requires a genuine 2xx from a known-good resource — a 404 means the path
        is wrong (or RESTCONF is half-up), not that the device is ready, so it
        must NOT count as reachable.
        """
        try:
            self.get(C.DATA_DEVICE_SYSTEM, timeout=C.GET_TIMEOUT, ok_404=False)
            return True
        except RestconfError:
            return False
