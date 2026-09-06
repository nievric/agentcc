"""Supported model providers and safe local-endpoint routing helpers."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from .models import ModelProvider, RegisteredModel


def provider_kind(model: RegisteredModel) -> ModelProvider:
    try:
        return ModelProvider(model.provider)
    except ValueError as error:
        raise ValueError(f"provider {model.provider!r} is not supported by AgentCC") from error


def is_local_gateway(model: RegisteredModel) -> bool:
    return provider_kind(model) is ModelProvider.LOCAL_GATEWAY


def endpoint_from_session(model: RegisteredModel) -> str:
    """Return a container-reachable endpoint for the selected model.

    A local service is normally registered as localhost on the host. Session
    containers have their own loopback interface, so translate only literal
    loopback hosts to Docker's host gateway alias. Other endpoints remain
    exactly as registered.
    """

    endpoint = model.endpoint.rstrip("/")
    if not is_local_gateway(model):
        return endpoint
    parsed = urlsplit(endpoint)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return endpoint
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, f"host.docker.internal{port}", parsed.path, parsed.query, parsed.fragment))
