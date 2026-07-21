"""Data models. ListingExtract is the schema the LLM must fill (drives
Gemini's guaranteed structured output). PipelineResult is what we compute."""
from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel, model_validator


class ListingExtract(BaseModel):
    """Exactly the fields the LLM extracts from a Hebrew post.
    Every unknown field is None — the model is told to never guess."""
    is_apartment_ad: bool
    price_per_room_ils: Optional[int] = None
    available_rooms_count: Optional[int] = None
    total_roommates_in_apt: Optional[int] = None
    street_address_or_neighborhood: Optional[str] = None
    lease_start_date: Optional[str] = None
    floor: Optional[str] = None            # as written ("קרקע", "3", "3 מתוך 5")
    furnished: Optional[bool] = None       # bed + table + closet per sleeping room
    balcony_or_garden: Optional[str] = None   # the specific one: "מרפסת" or "גינה" (else None)
    has_elevator: Optional[bool] = None    # מעלית — True/False/None (unmentioned)
    contact_phone_or_link: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_balcony(cls, data):
        # older archived parses used a bool has_balcony_or_garden — map a truthy one to
        # the combined label so they still score (+bonus) and display without a re-parse.
        if isinstance(data, dict) and not data.get("balcony_or_garden") \
                and data.get("has_balcony_or_garden"):
            data["balcony_or_garden"] = "מרפסת/גינה"
        return data
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
    score: Optional[int] = None           # 0–100 fit score (fit.py)
    location_tier: Optional[str] = None   # GREEN | AMBER | RED | UNKNOWN
    preferred: Optional[bool] = None      # True only for GREEN matches
    lat: Optional[float] = None
    lon: Optional[float] = None
    dedup_key: Optional[str] = None
    source_url: Optional[str] = None
    group: Optional[str] = None
    images: list[str] = []                 # apartment photos, for the alert album
    extract: Optional[ListingExtract] = None
