"""A small, dependency-free RESTCONF client for IOS-XE.

Uses only ``requests`` (always present in a Nautobot environment), so this job
can be delivered via a Git Repository without baking any extra Python package
into the Nautobot web/worker images.

The client is intentionally thin: GET for operational reads and POST for YANG
RPC ``operations``. All higher-level upgrade logic lives in ``iosxe_upgrade.py``.
"""

from __future__ import annotations

import requests

from . import constants as C


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
        except ValueError:
            return {}

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
        self._debug(f"POST {url} body={self._truncate(payload)}")
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
