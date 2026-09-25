"""方案负载的域校验。

所有校验错误都以 VALIDATION_ERROR 抛出，details 中携带稳定的细分代码；
校验失败时调用方不得写库，从而保证非法整版不会改写当前方案。
"""

import re

from .errors import ApiError

ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,32}")
PLAN_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_ZONES = 300
MAX_SEGMENTS = 2000
MAX_COST = 10**9


def _detail(code, field, message):
    return {"code": code, "field": field, "message": message}


def _fail(details):
    raise ApiError(422, "VALIDATION_ERROR", "plan payload is invalid", details)


def _is_valid_id(value):
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def validate_plan_payload(data):
    """校验并规范化方案负载，返回规范化字典；非法时抛出 ApiError。"""
    if not isinstance(data, dict):
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "request body must be a JSON object",
            [_detail("INVALID_BODY", "$", "expected a JSON object")],
        )
    details = []

    # ---- 区域（可选；缺省时从管段端点与污染源/保护区推导）----
    zones = []
    zone_set = set()
    declared = "zones" in data and data["zones"] is not None
    if declared:
        raw_zones = data["zones"]
        if not isinstance(raw_zones, list):
            details.append(
                _detail("INVALID_ZONES_FIELD", "zones", "must be a list of zone ids")
            )
            raw_zones = []
        for i, zone in enumerate(raw_zones):
            if not _is_valid_id(zone):
                details.append(
                    _detail(
                        "INVALID_ZONE_ID",
                        f"zones[{i}]",
                        "zone id must match [A-Za-z0-9_-]{1,32}",
                    )
                )
            elif zone in zone_set:
                details.append(
                    _detail(
                        "DUPLICATE_ZONE_ID",
                        f"zones[{i}]",
                        f"duplicate zone id {zone!r}",
                    )
                )
            else:
                zone_set.add(zone)
                zones.append(zone)

    # ---- 管段 ----
    segments = []
    seen_segment_ids = set()
    raw_segments = data.get("segments", [])
    if not isinstance(raw_segments, list):
        details.append(
            _detail("INVALID_SEGMENTS_FIELD", "segments", "must be a list of segments")
        )
        raw_segments = []
    for i, seg in enumerate(raw_segments):
        field = f"segments[{i}]"
        if not isinstance(seg, dict):
            details.append(_detail("INVALID_SEGMENT", field, "segment must be an object"))
            continue
        ok = True
        seg_id = seg.get("id")
        if not _is_valid_id(seg_id):
            details.append(
                _detail(
                    "INVALID_SEGMENT_ID",
                    f"{field}.id",
                    "segment id must match [A-Za-z0-9_-]{1,32}",
                )
            )
            ok = False
        elif seg_id in seen_segment_ids:
            details.append(
                _detail(
                    "DUPLICATE_SEGMENT_ID",
                    f"{field}.id",
                    f"duplicate segment id {seg_id!r}",
                )
            )
            ok = False
        frm, to = seg.get("from"), seg.get("to")
        for key, value in (("from", frm), ("to", to)):
            if not _is_valid_id(value):
                details.append(
                    _detail(
                        "INVALID_ZONE_REFERENCE",
                        f"{field}.{key}",
                        f"{key} must reference a zone id",
                    )
                )
                ok = False
        cost = seg.get("cost")
        # bool 是 int 的子类，必须显式排除
        if isinstance(cost, bool) or not isinstance(cost, int):
            details.append(
                _detail(
                    "INVALID_COST",
                    f"{field}.cost",
                    "cost must be an integer in [0, 10^9]",
                )
            )
            ok = False
        elif cost < 0 or cost > MAX_COST:
            details.append(
                _detail(
                    "INVALID_COST",
                    f"{field}.cost",
                    "cost must be an integer in [0, 10^9]",
                )
            )
            ok = False
        if ok:
            seen_segment_ids.add(seg_id)
            segments.append({"id": seg_id, "from": frm, "to": to, "cost": cost})

    # ---- 污染源 / 保护区 ----
    def zone_list(field, empty_code):
        raw = data.get(field)
        out = []
        if not isinstance(raw, list):
            details.append(
                _detail(
                    f"INVALID_{field.upper()}_FIELD",
                    field,
                    "must be a non-empty list of zone ids",
                )
            )
            return out
        if not raw:
            details.append(_detail(empty_code, field, "must not be empty"))
        seen = set()
        for i, zone in enumerate(raw):
            if not _is_valid_id(zone):
                details.append(
                    _detail(
                        "INVALID_ZONE_REFERENCE",
                        f"{field}[{i}]",
                        "must reference a zone id",
                    )
                )
            elif zone not in seen:
                seen.add(zone)
                out.append(zone)
        return out

    sources = zone_list("sources", "EMPTY_SOURCES")
    protections = zone_list("protections", "EMPTY_PROTECTIONS")

    # ---- 交叉校验 ----
    if declared:
        for i, seg in enumerate(segments):
            for key in ("from", "to"):
                if seg[key] not in zone_set:
                    details.append(
                        _detail(
                            "UNKNOWN_ZONE",
                            f"segments[{i}].{key}",
                            f"zone {seg[key]!r} is not declared",
                        )
                    )
        for field, values in (("sources", sources), ("protections", protections)):
            for zone in values:
                if zone not in zone_set:
                    details.append(
                        _detail(
                            "UNKNOWN_ZONE",
                            field,
                            f"zone {zone!r} is not declared",
                        )
                    )
    else:
        for seg in segments:
            for key in ("from", "to"):
                if seg[key] not in zone_set:
                    zone_set.add(seg[key])
                    zones.append(seg[key])
        for zone in sources + protections:
            if zone not in zone_set:
                zone_set.add(zone)
                zones.append(zone)

    if len(zones) > MAX_ZONES:
        details.append(
            _detail("TOO_MANY_ZONES", "zones", f"at most {MAX_ZONES} zones allowed")
        )
    if len(segments) > MAX_SEGMENTS:
        details.append(
            _detail(
                "TOO_MANY_SEGMENTS",
                "segments",
                f"at most {MAX_SEGMENTS} segments allowed",
            )
        )
    overlap = sorted(set(sources) & set(protections))
    if overlap:
        details.append(
            _detail(
                "SOURCE_PROTECTION_OVERLAP",
                "sources",
                f"zones declared as both source and protection: {overlap}",
            )
        )

    if details:
        _fail(details)
    return {
        "zones": zones,
        "segments": segments,
        "sources": sources,
        "protections": protections,
    }
