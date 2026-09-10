#!/usr/bin/env python3
"""Helpers for writing conditions tables in the canonical (relational) format.

Source of truth for the container format. ``beamline-simulation/psm/
psm_conditions.py`` is a copy of this file, kept there because a notebook in
that repo imports it by path; ``check_copies.py`` compares the two, and this is
the one to edit.

The JSON mirrors the shape the conditions database will store, so that one code
path in C++ resolves tags and intervals for both and the two cannot disagree:

    <name>_tag(tag, is_default, description)
    <name>_iov(row_id, tag, run_start, run_end, is_active, inserted_at, created_by)
    <name>_values(tag, channel_id | key, ordinal, <columns...>)

IMPORTANT: ``run_end`` is EXCLUSIVE. An interval covering runs 4700-4800
inclusive is written ``run_start=4700, run_end=4801``. ``None`` means
open-ended and mirrors a SQL NULL. This is the single easiest thing to get
wrong, which is why ``channel_table`` takes ``last_run`` and does the +1 itself.
"""
from __future__ import annotations

import json
from pathlib import Path


def channel_table(
    *,
    schema: str,
    tag: str,
    values: dict[int, dict[str, float]],
    version: int = 1,
    first_run: int = 0,
    last_run: int | None = None,
    description: str = "",
    created_by: str = "",
    comment: str = "",
    row_id: int = 1,
    is_default: bool = True,
) -> dict:
    """Build one channel-keyed conditions table.

    Args:
        values: channel id -> {column: value}
        first_run: first run this table applies to (inclusive).
        last_run: last run it applies to (INCLUSIVE), or None for open-ended.
                  Converted to the exclusive ``run_end`` internally.
        is_default: whether this tag is the table's default (False for an
                  alternative set selected explicitly, e.g. via a *Tag job
                  property).
    """
    return {
        "schema": schema,
        "version": version,
        "kind": "channel_values",
        "tags": [{"tag": tag, "is_default": bool(is_default), "description": description}],
        "iov": [
            {
                "row_id": row_id,
                "tag": tag,
                "run_start": first_run,
                # run_end is EXCLUSIVE; last_run is inclusive.
                "run_end": None if last_run is None else last_run + 1,
                "is_active": True,
                "created_by": created_by,
                "comment": comment,
            }
        ],
        "values": {
            tag: [
                {"channel_id": int(vid), **cols}
                for vid, cols in sorted(values.items())
            ]
        },
    }


def parameter_table(
    *,
    schema: str,
    tag: str,
    values: dict[str, object],
    version: int = 1,
    first_run: int = 0,
    last_run: int | None = None,
    description: str = "",
    created_by: str = "",
    comment: str = "",
    row_id: int = 1,
    is_default: bool = True,
) -> dict:
    """Build one parameter-set conditions table (kind "parameter_set").

    Args:
        values: parameter name -> value (scalars or lists; a list becomes
                ordinal cells on the C++ side).
        first_run / last_run: as in channel_table -- ``last_run`` is INCLUSIVE
                and converted to the exclusive ``run_end`` internally.
        is_default: as in channel_table.
    """
    return {
        "schema": schema,
        "version": version,
        "kind": "parameter_set",
        "tags": [{"tag": tag, "is_default": bool(is_default), "description": description}],
        "iov": [
            {
                "row_id": row_id,
                "tag": tag,
                "run_start": first_run,
                # run_end is EXCLUSIVE; last_run is inclusive.
                "run_end": None if last_run is None else last_run + 1,
                "is_active": True,
                "created_by": created_by,
                "comment": comment,
            }
        ],
        "values": {tag: [{"key": str(k), "value": v} for k, v in values.items()]},
    }


def write_container(path: str | Path, tables: dict[str, dict]) -> Path:
    """Write a conditions container: a JSON object of table name -> table."""
    path = Path(path)
    path.write_text(json.dumps(tables, indent=2) + "\n")
    return path
