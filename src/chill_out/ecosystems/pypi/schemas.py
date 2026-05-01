"""
Pydantic models describing the PyPI JSON API responses.

These mirror only the slice of fields chill-out actually consumes; everything
else in the wire payload is ignored. Validation runs strictly so any drift in
the registry's response shape (or any genuinely malformed row in our slice)
surfaces as a `ValidationError` that the backend wraps in `RegistryError`.

`requires_dist` entries are parsed through `packaging.requirements.Requirement`
at validation time; an unparsable entry fails the whole response. Markers
that gate a requirement on an `extra` are dropped (those represent optional
installs and don't constrain the base resolution).
"""

import datetime

from packaging.requirements import Requirement
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PypiArtifact(BaseModel):
    """One uploaded artifact (sdist or wheel) for a PyPI release.

    At least one of `upload_time_iso_8601` or `upload_time` must be present;
    a payload that strips both is treated as drift in the registry's response
    shape and surfaces as a `ValidationError`.

    `yanked` is the per-artifact withdraw signal. PyPI yanks individual files
    rather than whole versions on the wire; the backend collapses these into a
    single per-release flag (a release is yanked iff every artifact is yanked,
    matching `pip`/`uv` resolver behavior).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    upload_time_iso_8601: datetime.datetime | None = None
    upload_time: datetime.datetime | None = None
    yanked: bool = False

    @model_validator(mode="after")
    def _require_a_timestamp(self) -> "PypiArtifact":
        if self.upload_time_iso_8601 is None and self.upload_time is None:
            raise ValueError("artifact has neither upload_time_iso_8601 nor upload_time")
        return self


class PypiInfo(BaseModel):
    """The `info` block of a PyPI per-release JSON document.

    Only `requires_dist` is consumed; the field arrives as a list of PEP-508
    requirement strings or as `null` when the release declares no
    dependencies. Each entry is parsed eagerly through `Requirement` so any
    malformed row surfaces as a `ValidationError`.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    requires_dist: list[str] | None = None

    @field_validator("requires_dist")
    @classmethod
    def _parse_requirements(cls, raw: list[str] | None) -> list[str] | None:
        if raw is None:
            return None
        for entry in raw:
            # Constructing the Requirement validates the syntax; we keep the
            # original string in the model so downstream code can re-parse and
            # decide what to do with markers etc.
            Requirement(entry)
        return raw


class PypiPackageResponse(BaseModel):
    """Top-level response from `GET /<name>/json`.

    `releases` maps each version string to the list of artifacts uploaded for
    that version. Empty lists are valid (a version with no surviving uploads).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    releases: dict[str, list[PypiArtifact]] = Field(default_factory=dict)


class PypiVersionResponse(BaseModel):
    """Top-level response from `GET /<name>/<version>/json`."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    info: PypiInfo = Field(default_factory=PypiInfo)
