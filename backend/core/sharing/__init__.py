"""W31: per-document external share grants."""
from backend.core.sharing.grants import (
    ShareBundle,
    ShareGrant,
    ShareGrantInput,
    ShareGrantService,
)
from backend.core.sharing.resource_fetcher import (
    FetchedResource,
    FileArtifact,
    ResourceFetcher,
    get_resource_fetcher,
    reset_resource_fetcher,
)
from backend.core.sharing.scope import (
    ScopeViolation,
    check_resource_in_scope,
)
from backend.core.sharing.viewer_token import (
    ViewerClaims,
    decode_viewer_token,
    issue_viewer_token,
)

__all__ = [
    "FetchedResource",
    "FileArtifact",
    "ResourceFetcher",
    "ScopeViolation",
    "ShareBundle",
    "ShareGrant",
    "ShareGrantInput",
    "ShareGrantService",
    "ViewerClaims",
    "check_resource_in_scope",
    "decode_viewer_token",
    "get_resource_fetcher",
    "issue_viewer_token",
    "reset_resource_fetcher",
]
