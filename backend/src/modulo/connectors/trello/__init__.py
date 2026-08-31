"""TrelloConnector — async Trello REST API v1 connector."""

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)

_TRELLO_API = "https://api.trello.com/1"


class TrelloHTTPStatusError(httpx.HTTPStatusError):
    """``HTTPStatusError`` whose ``__str__`` redacts Trello credentials (FAR-507).

    ``httpx.HTTPStatusError.__str__`` renders the full message incl. the request
    URL — e.g. ``"Client error '401 Unauthorized' for url
    'https://api.trello.com/1/...?key=<key>&token=<token>'"``. Because the Trello
    client puts ``key``/``token`` in the request query string, the URL carries the
    LIVE credentials — and the shared ``sanitize_error_text`` does not match a
    bare ``token=<alnum>`` / ``key=<alnum>``, so they would otherwise persist
    verbatim into run/error detail. Subclassing lets the connector re-raise the
    SAME error family (callers catching ``httpx.HTTPStatusError`` keep working)
    with a redacted string rendering.
    """

    def __init__(
        self,
        message: str,
        *,
        request: httpx.Request,
        response: httpx.Response,
        redact: Callable[[str], str],
    ) -> None:
        super().__init__(message, request=request, response=response)
        self._redact = redact

    def __str__(self) -> str:
        return self._redact(super().__str__())


class TrelloHTTPError(httpx.HTTPError):
    """Transport ``httpx.HTTPError`` whose ``__str__`` redacts credentials.

    Covers the connect/timeout/read transport failures (never ``HTTPStatusError``
    — that is :class:`TrelloHTTPStatusError`) so a transport message that embeds
    the credential-bearing URL is still stripped before it propagates.
    """

    def __init__(self, message: str, *, redact: Callable[[str], str]) -> None:
        super().__init__(message)
        self._redact = redact

    def __str__(self) -> str:
        return self._redact(super().__str__())


class TrelloConnector(ConnectorBase):
    """Read/write Trello boards, lists, and cards via the REST API v1.

    Credentials (from credentials_ciphertext):
      "api_key"  — Trello API key
      "token"    — Trello API token

    Supported query resources:
      "boards"   — list boards for the authenticated user
      "lists"    — list lists on a board; filters: {"board_id": "..."}
      "cards"    — list cards on a board or list; filters: {"board_id": "..."} or {"list_id": "..."}
      "card"     — get a single card; filters: {"card_id": "..."}
      "members"  — list members on a board; filters: {"board_id": "..."}

    Supported write resources:
      "card"          — create a card; data: {"name": "...", "idList": "...", ...}
      "card_update"   — update a card; data: {"id": "...", ...}
      "comment"       — add a comment to a card; data: {"card_id": "...", "text": "..."}
    """

    def __init__(self, api_key: str, token: str) -> None:
        self._api_key = api_key
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.TRELLO

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_TRELLO_API,
            params={"key": self._api_key, "token": self._token},
            timeout=30,
        )

    # ── Credential redaction (FAR-507) ─────────────────────────────────────

    def _secret_values(self) -> list[str]:
        """Live credential values that must be stripped from any error text."""
        return [value for value in (self._api_key, self._token) if isinstance(value, str) and len(value) >= 4]

    def _redact(self, text: str) -> str:
        """Strip the live api_key + token from *text*.

        The credentials live in the request query string (``_client`` sets
        ``params={"key", "token"}``), so an httpx error message that echoes the
        request URL carries them verbatim. Stripping the actual values (not a
        regex) guarantees they can never survive into run/error detail,
        regardless of where they appear.
        """
        redacted = text
        for secret in self._secret_values():
            if secret and secret in redacted:
                redacted = redacted.replace(secret, "***")
        return redacted

    def _sanitize_status_error(self, exc: httpx.HTTPStatusError) -> TrelloHTTPStatusError:
        return TrelloHTTPStatusError(
            str(exc),
            request=exc.request,
            response=exc.response,
            redact=self._redact,
        )

    def _sanitize_transport_error(self, exc: httpx.HTTPError) -> TrelloHTTPError:
        return TrelloHTTPError(str(exc), redact=self._redact)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        raise_on_status: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """Issue a Trello request, sanitising httpx errors of the credentials.

        Every request URL carries ``?key=<api_key>&token=<token>`` (client-level
        params), so a raw ``httpx.HTTPStatusError``/transport message would leak
        the LIVE credentials into run/error detail. This wrapper re-raises a
        redacted exception so ``str(exc)`` is clean; the original is preserved as
        the ``__cause__``.
        """
        async with self._client() as client:
            try:
                resp = await client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise self._sanitize_transport_error(exc) from exc
            if raise_on_status:
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise self._sanitize_status_error(exc) from exc
        return resp

    async def health_check(self) -> HealthResult:
        """Verify connectivity by fetching the authenticated user's profile."""
        r = await self._request("GET", "/members/me", raise_on_status=False)

        if r.status_code != 200:
            return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

        body: dict[str, Any] = r.json()
        if "id" not in body:
            return HealthResult(ok=False, detail="Unexpected response — no 'id' in member profile")

        display_name = body.get("fullName") or body.get("username") or ""
        return HealthResult(ok=True, detail=display_name)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        handler = self._query_handlers().get(q.resource)
        if handler is None:
            raise ValueError(f"Unsupported Trello resource: {q.resource!r}")
        return await handler(q)

    def _query_handlers(
        self,
    ) -> dict[str, Callable[[ConnectorQuery], Awaitable[ConnectorResult]]]:
        return {
            "boards": self._query_boards,
            "lists": self._query_lists,
            "cards": self._query_cards,
            "card": self._query_card,
            "members": self._query_members,
        }

    async def _query_boards(self, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, str] = {}
        params.update(self._filter_param(q))
        params.update(self._fields_param(q))
        r = await self._request("GET", "/members/me/boards", params=params)
        boards: list[dict[str, Any]] = r.json()
        return ConnectorResult(records=boards, total=len(boards))

    async def _query_lists(self, q: ConnectorQuery) -> ConnectorResult:
        if "board_id" not in q.filters:
            raise ValueError("Trello lists query requires 'board_id' filter")
        board_id = q.filters["board_id"]
        params = self._filter_param(q)
        r = await self._request("GET", f"/boards/{board_id}/lists", params=params)
        lists: list[dict[str, Any]] = r.json()
        return ConnectorResult(records=lists, total=len(lists))

    async def _query_cards(self, q: ConnectorQuery) -> ConnectorResult:
        params = self._fields_param(q)
        r = await self._request("GET", self._cards_endpoint(q), params=params)
        cards: list[dict[str, Any]] = r.json()
        return ConnectorResult(records=cards, total=len(cards))

    async def _query_card(self, q: ConnectorQuery) -> ConnectorResult:
        if "card_id" not in q.filters:
            raise ValueError("Trello card query requires 'card_id' filter")
        card_id = q.filters["card_id"]
        r = await self._request("GET", f"/cards/{card_id}")
        card: dict[str, Any] = r.json()
        return ConnectorResult(records=[card])

    async def _query_members(self, q: ConnectorQuery) -> ConnectorResult:
        if "board_id" not in q.filters:
            raise ValueError("Trello members query requires 'board_id' filter")
        board_id = q.filters["board_id"]
        r = await self._request("GET", f"/boards/{board_id}/members")
        members: list[dict[str, Any]] = r.json()
        return ConnectorResult(records=members, total=len(members))

    @staticmethod
    def _cards_endpoint(q: ConnectorQuery) -> str:
        board_id = q.filters.get("board_id")
        list_id = q.filters.get("list_id")
        if board_id:
            return f"/boards/{board_id}/cards"
        if list_id:
            return f"/lists/{list_id}/cards"
        raise ValueError("Trello cards query requires 'board_id' or 'list_id' filter")

    @staticmethod
    def _filter_param(q: ConnectorQuery) -> dict[str, str]:
        if "filter" in q.filters:
            return {"filter": q.filters["filter"]}
        return {}

    @staticmethod
    def _fields_param(q: ConnectorQuery) -> dict[str, str]:
        if "fields" in q.filters:
            return {"fields": q.filters["fields"]}
        return {}

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        match payload.resource:
            case "card":
                r = await self._request("POST", "/cards", json=payload.data)
                return r.json()

            case "card_update":
                if "id" not in payload.data:
                    raise ValueError("Trello card_update requires 'id' in data")
                card_id = payload.data["id"]
                r = await self._request("PUT", f"/cards/{card_id}", json=payload.data)
                return r.json()

            case "comment":
                if "card_id" not in payload.data:
                    raise ValueError("Trello comment requires 'card_id' in data")
                card_id = payload.data["card_id"]
                text = payload.data.get("text", "")
                r = await self._request("POST", f"/cards/{card_id}/actions/comments", json={"text": text})
                return r.json()

            case _:
                raise ValueError(f"Unsupported Trello write resource: {payload.resource!r}")
