"""Pure helpers for meeting class and attendee metric bucketing."""

VALID_RESPONSES = frozenset({
    "accepted",
    "declined",
    "tentative",
    "needsAction",
})


def parse_class_pairs(pairs):
    """Parse ['className=value', ...] into a list of (className, value) tuples."""
    parsed = []
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"Expected className=value, got: {pair!r}")
        class_name, value = pair.split("=", 1)
        class_name = class_name.strip()
        value = value.strip()
        if not class_name or not value:
            raise ValueError(f"Expected className=value, got: {pair!r}")
        parsed.append((class_name, value))
    return parsed


def build_color_map(pairs):
    """Build colorId -> className map; first mapping for a colorId wins."""
    color_map = {}
    for class_name, color_id in parse_class_pairs(pairs):
        if color_id not in color_map:
            color_map[color_id] = class_name
    return color_map


def classify_event(event, prefix_rules, color_map):
    """Return meeting class from title prefix (first match), else colour, else unclassified."""
    summary = event.get("summary") or ""
    summary_lower = summary.lower()
    for class_name, prefix in prefix_rules:
        if summary_lower.startswith(prefix.lower()):
            return class_name

    color_id = event.get("colorId")
    if color_id is not None:
        color_key = str(color_id)
        if color_key in color_map:
            return color_map[color_key]

    return "unclassified"


def attendee_buckets(event):
    """Yield (optionality, response) for each non-self, non-resource attendee."""
    for attendee in event.get("attendees") or []:
        if attendee.get("self") or attendee.get("resource"):
            continue
        optionality = "optional" if attendee.get("optional") else "mandatory"
        response = attendee.get("responseStatus", "needsAction")
        if response not in VALID_RESPONSES:
            response = "needsAction"
        yield (optionality, response)
