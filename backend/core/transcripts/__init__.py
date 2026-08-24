# -*- coding: utf-8 -*-
"""Transcripts processing — speaker resolution, attendees, diarization.

issue #112 T-6: self-introduction detection без LLM-вызовов.
issue #112 T-4/T-5: attendees resolution + assignee validation.
"""
from backend.core.transcripts.attendees import (
    attendee_names,
    is_assignee_in_attendees,
    normalize_attendees,
    resolve_attendees,
)
from backend.core.transcripts.speaker_resolver import (
    extract_speaker_introductions,
    resolve_speakers_for_extraction,
)

__all__ = [
    "attendee_names",
    "extract_speaker_introductions",
    "is_assignee_in_attendees",
    "normalize_attendees",
    "resolve_attendees",
    "resolve_speakers_for_extraction",
]
