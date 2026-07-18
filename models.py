"""Data models. ListingExtract is the schema the LLM must fill (drives
Gemini's guaranteed structured output). PipelineResult is what we compute."""
from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel


class ListingExtract(BaseModel):
    """Exactly the fields the LLM extracts from a Hebrew post.
    Every unknown field is None — the model is told to never guess."""
    is_apartment_ad: bool
    price_per_room_ils: Optional[int] = None
    available_rooms_count: Optional[int] = None
    total_roommates_in_apt: Optional[int] = None
    street_address_or_neighborhood: Optional[str] = None
    lease_start_date: Optional[str] = None
    contact_phone_or_link: Optional[str] = None
    missing_critical_data: bool = False
    price_from_comment: bool = False       # price came from a comment, not the post
    summary_hebrew: Optional[str] = None


class Status(str, Enum):
    MATCH = "MATCH"           # passes every hard gate
    NEEDS_DATA = "NEEDS_DATA"  # apartment ad, but missing critical fields
    DROP = "DROP"             # fails a hard gate
    NOT_AD = "NOT_AD"         # not an apartment rental at all


class PipelineResult(BaseModel):
    status: Status
    reason: str = ""                 # why it was dropped / flagged
    walk_minutes: Optional[float] = None
    walk_gate: Optional[str] = None       # name of the nearest campus gate
    location_tier: Optional[str] = None   # GREEN | AMBER | RED | UNKNOWN
    preferred: Optional[bool] = None      # True only for GREEN matches
    lat: Optional[float] = None
    lon: Optional[float] = None
    dedup_key: Optional[str] = None
    source_url: Optional[str] = None
    group: Optional[str] = None
    images: list[str] = []                 # apartment photos, for the alert album
    extract: Optional[ListingExtract] = None
