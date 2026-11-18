"""Bind killfeed attacker, victim and map position before resolving a target ID."""

import math
import re
import unicodedata

ERRORS = {
    "killfeed_missing",
    "combat_log_invalid",
    "killfeed_geometry_invalid",
    "killfeed_roles_invalid",
    "victim_position_unproven",
    "victim_link_unproven",
    "killer_identity_ambiguous",
    "killer_target_conflict",
    "combat_refs_invalid",
    "combat_confidence_insufficient",
    "killer_position_unproven",
    "killer_link_unproven",
    "combat_event_unproven",
    "full_death_view_unproven",
    "damage_log_conflict",
}


def normalized_name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise ValueError("killfeed_roles_invalid")
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def box(value):
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("killfeed_geometry_invalid")
    if any(
        isinstance(n, bool)
        or not isinstance(n, (int, float))
        or not math.isfinite(n)
        or not 0 <= n <= 1
        for n in value
    ):
        raise ValueError("killfeed_geometry_invalid")
    if value[0] >= value[2] or value[1] >= value[3]:
        raise ValueError("killfeed_geometry_invalid")
    return value


def score(value, minimum):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not minimum <= value <= 1
    ):
        raise ValueError("combat_confidence_insufficient")


def bind_combat_evidence(decision, evidence_ids, players, reporter_context=None):
    if decision.get("rule_id") != "outside_combat":
        return decision
    c = decision.get("combat_evidence")
    if not isinstance(c, dict):
        raise ValueError("killfeed_missing")
    feed, pos = c.get("killfeed_ref"), c.get("position_ref")
    if not all(isinstance(ref, str) and ref in evidence_ids for ref in (feed, pos)):
        raise ValueError("combat_refs_invalid")
    refs = decision.get("evidence_refs")
    if not isinstance(refs, list) or feed not in refs or pos not in refs:
        raise ValueError("combat_refs_invalid")
    killer, victim = (
        normalized_name(c.get("killer_name")),
        normalized_name(c.get("victim_name")),
    )
    if killer == victim:
        raise ValueError("killfeed_roles_invalid")
    source_kind = c.get("combat_source", "killfeed")
    if source_kind == "event_log":
        bind_event_log(c, killer, victim, feed, pos)
    elif source_kind == "killfeed":
        left, gun, right = (
            box(c.get("killer_bbox")),
            box(c.get("weapon_bbox")),
            box(c.get("victim_bbox")),
        )
        # All three boxes must refer to the same horizontal killfeed row.
        if not (
            left[2] <= gun[0]
            and gun[2] <= right[0]
            and max(left[1], gun[1], right[1]) < min(left[3], gun[3], right[3])
        ):
            raise ValueError("killfeed_geometry_invalid")
    else:
        raise ValueError("combat_log_invalid")
    if c.get("position_subject_role") == "killer":
        bind_killer_position(c, killer, feed, pos)
    else:
        basis = c.get("victim_link_basis")
        first_person_link = False
        if feed == pos:
            if basis == "first_person_death_view":
                bind_first_person_death(c, pos)
                first_person_link = True
            elif basis != "same_frame_death_view":
                raise ValueError("victim_link_unproven")
        elif basis == "reporter_nickname_first_person":
            bind_first_person_death(c, pos)
            context = reporter_context if isinstance(reporter_context, dict) else {}
            name = context.get("display_name")
            own = context.get("own_evidence_refs")
            if (
                not isinstance(name, str)
                or not name.strip()
                or normalized_name(name) != victim
                or not isinstance(own, list)
                or feed not in own
                or pos not in own
                or c.get("same_event_basis") != "visible_event_context"
                or not isinstance(c.get("same_event_refs"), list)
                or feed not in c["same_event_refs"]
                or pos not in c["same_event_refs"]
            ):
                raise ValueError("victim_link_unproven")
            score(c.get("same_event_confidence"), 0.99)
            first_person_link = True
        else:
            # Other authors and unknown quoted images cannot borrow the reporter name.
            if basis != "visible_victim_name" or c.get("position_identity_ref") != pos:
                raise ValueError("victim_link_unproven")
            box(c.get("position_name_bbox"))
        if first_person_link and c.get("position_subject_name") in (None, ""):
            c = dict(
                c,
                position_subject_name=c["victim_name"],
                position_subject_name_source="killfeed_victim_first_person",
            )
            decision = dict(decision, combat_evidence=c)
        subject = c.get("position_subject_name")
        if (
            not isinstance(subject, str)
            or not subject.strip()
            or normalized_name(subject) != victim
        ):
            raise ValueError("victim_link_unproven")
        score(c.get("killfeed_confidence"), 0.99)
        score(c.get("position_confidence"), 0.95)
        score(c.get("victim_link_confidence"), 0.99)
        if (
            c.get("victim_outside_yellow") is not True
            or c.get("position_subject_role") != "victim_self"
        ):
            raise ValueError("victim_position_unproven")
        if c.get("outside_basis") == "complete_death_minimap_no_yellow":
            bind_full_death_view(c, killer, feed, pos)
        else:
            if (
                c.get("victim_marker_type")
                not in ("white_ecg_circle", "self_ecg_view_range")
                or c.get("yellow_boundary_visible") is not True
            ):
                raise ValueError("victim_position_unproven")
            box(c.get("victim_marker_bbox"))
            if c.get("victim_marker_type") == "self_ecg_view_range":
                bind_self_view_range(c, pos)
    matches = []
    for p in players:
        name = p.get("name")
        if isinstance(name, str) and name.strip() and normalized_name(name) == killer:
            matches.append(p)
    if len(matches) != 1 or not re.fullmatch(
        r"[0-9]{17}", str(matches[0].get("steamId", ""))
    ):
        raise ValueError("killer_identity_ambiguous")
    target = str(matches[0]["steamId"])
    if decision.get("target_steam_id") not in ("", None, target):
        raise ValueError("killer_target_conflict")
    result = dict(decision, target_steam_id=target)
    result["identity_evidence"] = list(
        dict.fromkeys([feed, *decision.get("identity_evidence", [])])
    )
    return result


def bind_killer_position(c, killer, feed, pos):
    """A named attacker outside is sufficient; the victim can be inside."""
    subject = c.get("position_subject_name")
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or normalized_name(subject) != killer
        or c.get("position_link_basis") != "visible_killer_name"
        or c.get("position_identity_ref") != pos
    ):
        raise ValueError("killer_link_unproven")
    box(c.get("position_name_bbox"))
    box(c.get("position_marker_bbox"))
    score(c.get("killfeed_confidence"), 0.99)
    score(c.get("position_confidence"), 0.95)
    score(c.get("position_link_confidence"), 0.99)
    score(c.get("marker_name_link_confidence"), 0.99)
    if (
        c.get("position_outside_yellow") is not True
        or c.get("position_marker_type") != "named_player_marker"
        or c.get("yellow_boundary_visible") is not True
    ):
        raise ValueError("killer_position_unproven")
    if feed != pos:
        refs = c.get("same_event_refs")
        if (
            c.get("same_event_basis") != "visible_event_context"
            or not isinstance(refs, list)
            or feed not in refs
            or pos not in refs
        ):
            raise ValueError("combat_event_unproven")
        score(c.get("same_event_confidence"), 0.99)


def bind_full_death_view(c, killer, feed, pos):
    """Trusted UI rule: a complete death-view minimap with no yellow is outside."""
    if (
        feed != pos
        or c.get("full_death_view_ref") != pos
        or c.get("minimap_ref") != pos
        or c.get("full_death_view_confirmed") is not True
        or c.get("minimap_complete") is not True
        or c.get("minimap_readable") is not True
        or c.get("yellow_region_present") is not False
        or c.get("yellow_arc_present") is not False
        or c.get("yellow_boundary_visible") is not False
        or c.get("victim_marker_type") != "death_view_minimap"
    ):
        raise ValueError("full_death_view_unproven")
    box(c.get("minimap_bbox"))
    score(c.get("full_death_view_confidence"), 0.99)
    score(c.get("minimap_confidence"), 0.99)
    # HZ is distance to the center, not a boundary distance; never threshold it.
    if c.get("damage_log_ref") or c.get("damage_log_attacker_name"):
        name = c.get("damage_log_attacker_name")
        if (
            c.get("damage_log_ref") != pos
            or not isinstance(name, str)
            or not name.strip()
            or normalized_name(name) != killer
        ):
            raise ValueError("damage_log_conflict")
        box(c.get("damage_log_bbox"))


def bind_first_person_death(c, pos):
    """Visible death UI or own white death marker establishes the view subject."""
    if (
        c.get("first_person_death_confirmed") is not True
        or c.get("first_person_death_ref") != pos
        or c.get("first_person_death_basis")
        not in ("death_ui", "white_self_death_marker", "self_ecg_view_range")
        or c.get("position_subject_role") != "victim_self"
    ):
        raise ValueError("victim_link_unproven")
    box(c.get("first_person_death_bbox"))
    score(c.get("first_person_death_confidence"), 0.99)
    if c["first_person_death_basis"] in (
        "white_self_death_marker",
        "self_ecg_view_range",
    ):
        expected = (
            "white_ecg_circle"
            if c["first_person_death_basis"] == "white_self_death_marker"
            else "self_ecg_view_range"
        )
        if c.get("victim_marker_type") != expected:
            raise ValueError("victim_link_unproven")
        marker = box(c.get("victim_marker_bbox"))
        anchor = box(c.get("first_person_death_bbox"))
        if max(marker[0], anchor[0]) >= min(marker[2], anchor[2]) or max(
            marker[1], anchor[1]
        ) >= min(marker[3], anchor[3]):
            raise ValueError("victim_link_unproven")
        if c["first_person_death_basis"] == "self_ecg_view_range":
            bind_self_view_range(c, pos)


def bind_self_view_range(c, pos):
    """The own ECG marker has a visible surrounding view range, not just color."""
    if (
        c.get("self_view_range_ref") != pos
        or c.get("self_view_range_confirmed") is not True
    ):
        raise ValueError("victim_position_unproven")
    marker = box(c.get("victim_marker_bbox"))
    halo = box(c.get("self_view_range_bbox"))
    score(c.get("self_view_range_confidence"), 0.99)
    if not (
        halo[0] <= marker[0] < marker[2] <= halo[2]
        and halo[1] <= marker[1] < marker[3] <= halo[3]
        and (halo[2] - halo[0]) * (halo[3] - halo[1])
        > (marker[2] - marker[0]) * (marker[3] - marker[1])
    ):
        raise ValueError("victim_position_unproven")


def bind_event_log(c, killer, victim, feed, pos):
    """Accept an explicit kill row; damage and economy entries are not kills."""
    if c.get("combat_log_ref") != feed or c.get("combat_log_event_kind") != "kill":
        raise ValueError("combat_log_invalid")
    box(c.get("combat_log_bbox"))
    text = c.get("combat_log_text")
    if not isinstance(text, str) or len(text) > 240:
        raise ValueError("combat_log_invalid")
    # OCR text must preserve names and order. Only the optional clock is omitted.
    row = re.sub(r"^\s*\[[0-9:. ]+\]\s*", "", text).strip()
    row = row.rstrip("。.!！").strip()
    if normalized_name(row) != killer + "击杀了" + victim:
        raise ValueError("combat_log_invalid")
    if feed != pos:
        refs = c.get("same_event_refs")
        if (
            c.get("same_event_basis") != "visible_event_context"
            or not isinstance(refs, list)
            or feed not in refs
            or pos not in refs
        ):
            raise ValueError("combat_event_unproven")
        score(c.get("same_event_confidence"), 0.99)
