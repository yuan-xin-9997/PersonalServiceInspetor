from __future__ import annotations

from urllib.parse import urlparse

from fastapi import HTTPException
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import delete

from .ai_service import digest, now
from .database import AIClient, AISecret


class InspectorOAuth:
    """SDK supplies OAuth routing, redirect validation, client authentication and S256 PKCE."""

    def __init__(self, service, base_url: str):
        self.service = service
        self.db = service.db
        self.base = base_url.rstrip("/")
        self.resource = self.base + "/mcp"

    async def get_client(self, client_id: str):
        with self.db.session() as session:
            row = session.get(AIClient, client_id)
            return OAuthClientInformationFull.model_validate(row.payload) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull):
        # Public clients use S256 PKCE; no recoverable client secrets are needed or stored.
        if client_info.token_endpoint_auth_method != "none" or client_info.client_secret:
            raise RegistrationError(
                "invalid_client_metadata",
                "Only public PKCE clients (auth method none) are supported",
            )
        for uri in client_info.redirect_uris or []:
            parsed = urlparse(str(uri))
            if parsed.fragment or not (
                parsed.scheme == "https"
                or (
                    parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                )
            ):
                raise RegistrationError(
                    "invalid_redirect_uri", "Use HTTPS or loopback HTTP redirect URIs"
                )
        with self.db.session() as session:
            session.add(
                AIClient(id=client_info.client_id, payload=client_info.model_dump(mode="json"))
            )

    async def authorize(self, client, params: AuthorizationParams) -> str:
        if params.resource != self.resource:
            raise AuthorizeError("invalid_request", "resource must identify this MCP endpoint")
        pending = self.service.save_secret(
            "pending",
            {"client_id": client.client_id, "params": params.model_dump(mode="json")},
            now() + 600,
        )
        return self.base + "/ai/consent/" + pending

    def load(self, secret: str, kind: str, consume=False):
        with self.db.session() as session:
            if consume:
                row = session.execute(
                    delete(AISecret)
                    .where(
                        AISecret.digest == digest(secret),
                        AISecret.kind == kind,
                        AISecret.expires_at > now(),
                    )
                    .returning(AISecret)
                ).scalar_one_or_none()
            else:
                row = session.get(AISecret, digest(secret))
            if not row or row.kind != kind or row.expires_at <= now():
                return None
            return row

    def consent(self, pending: str, username: str, selected: list[str], days: int) -> str:
        row = self.load(pending, "pending", consume=True)
        if not row:
            raise HTTPException(400, "授权请求已过期或已使用")
        params = AuthorizationParams.model_validate(row.payload["params"])
        if not set(selected) <= set(params.scopes or ["status:read"]):
            raise HTTPException(403, "不能扩展客户端请求的权限")
        credential = self.service.create_credential(
            username, "OAuth: " + row.payload["client_id"][:40], selected, now() + days * 86400
        )
        code_payload = {
            "scopes": selected,
            "client_id": row.payload["client_id"],
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": self.resource,
            "subject": username,
        }
        code = self.service.save_secret("code", code_payload, now() + 120, credential["id"])
        return construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state, iss=self.base
        )

    async def load_authorization_code(self, client, authorization_code):
        row = self.load(authorization_code, "code")
        if row and row.payload["client_id"] == client.client_id:
            return AuthorizationCode(
                code=authorization_code, expires_at=row.expires_at, **row.payload
            )
        return None

    async def exchange_authorization_code(self, client, authorization_code):
        row = self.load(authorization_code.code, "code", consume=True)
        if not row or row.payload["client_id"] != client.client_id:
            raise TokenError("invalid_grant", "Code already used or expired")
        return self.tokens(row, client.client_id)

    def tokens(self, row, client_id, scopes=None):
        try:
            p = self.service.credential_principal(
                row.credential_id, scopes if scopes is not None else row.payload["scopes"]
            )
            return OAuthToken(
                **self.service.issue_tokens(p, client_id, self.resource, refresh=True)
            )
        except HTTPException as exc:
            raise TokenError("invalid_grant", "Credential or user no longer active") from exc

    async def load_refresh_token(self, client, refresh_token):
        row = self.load(refresh_token, "refresh")
        if row and row.payload["client_id"] == client.client_id:
            try:
                principal = self.service.credential_principal(row.credential_id)
            except HTTPException:
                return None
            return RefreshToken(
                token=refresh_token,
                expires_at=row.expires_at,
                subject=principal.username,
                **row.payload,
            )
        return None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        row = self.load(refresh_token.token, "refresh", consume=True)
        if not row or row.payload["client_id"] != client.client_id:
            raise TokenError("invalid_grant", "Refresh token already used or expired")
        return self.tokens(row, client.client_id, scopes)

    async def load_access_token(self, token):
        row = self.load(token, "access")
        if not row:
            return None
        try:
            p = self.service.authenticate(token, allow_key=False)
        except HTTPException:
            return None
        return AccessToken(
            token=token,
            client_id=row.payload["client_id"],
            scopes=sorted(p.scopes),
            expires_at=row.expires_at,
            resource=row.payload["resource"],
            subject=p.username,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken):
        kind = "access" if isinstance(token, AccessToken) else "refresh"
        row = self.load(token.token, kind)
        if row:
            p = self.service.credential_principal(row.credential_id)
            self.service.revoke(p.username, p.credential_id)

    async def exchange_identity_assertion(self, client, params):
        raise TokenError("unsupported_grant_type", "Identity assertions are not enabled")
