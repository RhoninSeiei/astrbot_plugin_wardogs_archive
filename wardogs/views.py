"""Pixel-derived visual aids; every view retains its original evidence identity."""

import base64
import copy
import io
import warnings

from PIL import Image, ImageOps

from .combat import box


def _encode(image):
    factor = min(3.0, 1600 / max(image.size))
    size = tuple(max(1, round(n * factor)) for n in image.size)
    if size != image.size:
        image = image.resize(size, Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def prepare_views(images):
    if not 1 <= len(images) <= 4:
        raise ValueError("invalid_image_count")
    views = {}
    meta = {}
    extras = []
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        for source, uri in images.items():
            if (
                not isinstance(uri, str)
                or not uri.startswith("data:image/")
                or ";base64," not in uri
            ):
                raise ValueError("invalid_image_data")
            raw = base64.b64decode(uri.split(",", 1)[1], validate=True)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("image_too_large")
            with Image.open(io.BytesIO(raw)) as original:
                if original.width * original.height > 16_000_000:
                    raise ValueError("image_dimensions_too_large")
                image = ImageOps.exif_transpose(original).convert("RGB")
            w, h = image.size
            views[source] = _encode(image)
            meta[source] = {
                "source_ref": source,
                "crop": [0, 0, 1, 1],
                "purpose": "original",
            }
            rects = {}
            if h >= 600 and h > w * 1.2:
                rects = {
                    "killfeed": (0, 0, w, round(h * 0.27)),
                    "map": (0, round(h * 0.45), round(w * 0.65), h),
                    "log": (0, round(h * 0.45), w, round(h * 0.8)),
                }
            elif h >= 600 and w > h * 1.2:
                rects = {
                    "killfeed": (0, round(h * 0.3), round(w * 0.4), round(h * 0.6)),
                    "map": (0, round(h * 0.65), round(w * 0.22), h),
                    "damage": (round(w * 0.8), round(h * 0.6), w, round(h * 0.82)),
                }
            for purpose, rect in rects.items():
                ref = source + ":" + purpose
                extras.append(
                    (
                        ref,
                        _encode(image.crop(rect)),
                        {
                            "source_ref": source,
                            "crop": [
                                rect[0] / w,
                                rect[1] / h,
                                rect[2] / w,
                                rect[3] / h,
                            ],
                            "purpose": purpose + "_detail",
                        },
                    )
                )
        for ref, uri, info in extras:
            if len(views) >= 8:
                break
            views[ref] = uri
            meta[ref] = info
    if sum(len(s) for s in views.values()) > 32 * 1024 * 1024:
        raise ValueError("vision_batch_too_large")
    return views, meta


def normalize_view_refs(decision, meta):
    d = copy.deepcopy(decision)
    used = set()

    def source(ref):
        if isinstance(ref, str) and ref in meta:
            used.add(ref)
            return meta[ref]["source_ref"]
        return ref

    def mapped_box(value, ref):
        if ref not in meta or value == []:
            return value
        value = box(value)
        crop = meta[ref]["crop"]
        x, y, x2, y2 = crop
        return [
            x + value[0] * (x2 - x),
            y + value[1] * (y2 - y),
            x + value[2] * (x2 - x),
            y + value[3] * (y2 - y),
        ]

    for field in ["evidence_refs", "identity_evidence"]:
        if isinstance(d.get(field), list):
            if not all(isinstance(r, str) for r in d[field]):
                raise ValueError("invalid_evidence_refs")
            d[field] = list(dict.fromkeys(source(ref) for ref in d[field]))
    c = d.get("combat_evidence")
    if isinstance(c, dict):
        feed, pos, name = (
            c.get("killfeed_ref"),
            c.get("position_ref"),
            c.get("position_identity_ref"),
        )
        for field in ["killer_bbox", "weapon_bbox", "victim_bbox"]:
            if field in c:
                c[field] = mapped_box(c[field], feed)
        if "victim_marker_bbox" in c:
            c["victim_marker_bbox"] = mapped_box(c["victim_marker_bbox"], pos)
        if "position_marker_bbox" in c:
            c["position_marker_bbox"] = mapped_box(c["position_marker_bbox"], pos)
        if isinstance(c.get("same_event_refs"), list):
            c["same_event_refs"] = list(
                dict.fromkeys(source(ref) for ref in c["same_event_refs"])
            )
        if "position_name_bbox" in c:
            c["position_name_bbox"] = mapped_box(c["position_name_bbox"], name)
        for bbox_field, ref_field in [
            ("minimap_bbox", "minimap_ref"),
            ("first_person_death_bbox", "first_person_death_ref"),
            ("damage_log_bbox", "damage_log_ref"),
            ("combat_log_bbox", "combat_log_ref"),
            ("self_view_range_bbox", "self_view_range_ref"),
        ]:
            if bbox_field in c:
                c[bbox_field] = mapped_box(c[bbox_field], c.get(ref_field))
        for field in [
            "killfeed_ref",
            "position_ref",
            "position_identity_ref",
            "full_death_view_ref",
            "first_person_death_ref",
            "minimap_ref",
            "damage_log_ref",
            "combat_log_ref",
            "self_view_range_ref",
        ]:
            if field in c:
                c[field] = source(c[field])
    d["derived_views_used"] = {ref: meta[ref] for ref in sorted(used)}
    return d
