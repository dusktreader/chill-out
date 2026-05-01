"""
Pydantic models describing the npm registry's JSON responses.

These mirror only the slice of fields chill-out actually consumes; everything
else in the wire payload is ignored. Validation runs strictly so any drift in
the registry's response shape (or any genuinely malformed row in our slice)
surfaces as a `ValidationError` that the backend wraps in `RegistryError`.
"""

import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NpmPackageResponse(BaseModel):
    """Outer envelope returned by `GET /<name>` on the npm registry.

    The `time` map carries one entry per published version plus the
    bookkeeping keys `created` and `modified`. Backends drop the bookkeeping
    keys when materializing the per-release timestamps.

    `versions` is the per-version document map. npm keeps unpublished
    versions in `time` for history but removes them from `versions`; the
    backend uses the difference to flag yanked releases. The per-version
    payload is opaque here -- chill-out fetches the full version document
    separately when it needs dependency declarations.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    time: dict[str, datetime.datetime] = Field(default_factory=dict)
    versions: dict[str, Any] = Field(default_factory=dict)


class NpmVersionResponse(BaseModel):
    """Per-version document returned by `GET /<name>/<version>`.

    chill-out only reads the dependency declarations; everything else (tarball
    URLs, integrity hashes, scripts, and so on) is ignored.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    dependencies: dict[str, str] = Field(default_factory=dict)
    peerDependencies: dict[str, str] = Field(default_factory=dict)
