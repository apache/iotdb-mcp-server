import re


AGGREGATE_FUNCTIONS = (
    "COUNT",
    "SUM",
    "AVG",
    "MAX_VALUE",
    "MIN_VALUE",
    "FIRST_VALUE",
    "LAST_VALUE",
    "MAX",
    "MIN",
)


def _split_top_level_csv(expr: str) -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    in_single_quote = False

    idx = 0
    while idx < len(expr):
        ch = expr[idx]
        if ch == "'":
            in_single_quote = not in_single_quote
            cur.append(ch)
        elif not in_single_quote and ch == "(":
            depth += 1
            cur.append(ch)
        elif not in_single_quote and ch == ")":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif not in_single_quote and ch == "," and depth == 0:
            part = "".join(cur).strip()
            if part:
                parts.append(part)
            cur = []
        else:
            cur.append(ch)
        idx += 1

    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _strip_alias(expr: str) -> str:
    out = re.sub(r"\s+AS\s+[A-Z_][A-Z0-9_]*\s*$", "", expr, flags=re.IGNORECASE)
    m = re.match(r"^(.*\S)\s+([A-Z_][A-Z0-9_]*)\s*$", out, flags=re.IGNORECASE)
    if m and "(" in m.group(1):
        return m.group(1)
    return out


def _strip_identifier_quotes(identifier: str) -> str:
    value = identifier.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"`", '"'}:
        return value[1:-1].strip()
    return value


def _extract_projection_expr(sql: str) -> str:
    match = re.search(
        r"\bSELECT\b\s+(?:LAST\s+)?(.+?)\s+\bFROM\b",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    return match.group(1).strip()


def _extract_from_identifier(sql: str) -> str:
    match = re.search(r"\bFROM\s+([^\s,;]+)", sql, flags=re.IGNORECASE)
    if not match:
        return ""
    identifier = match.group(1).strip()
    if not identifier or identifier.startswith("("):
        return ""
    return _strip_identifier_quotes(identifier.rstrip(",;"))


def _projection_measurement_names(sql: str) -> set[str]:
    projection = _extract_projection_expr(sql)
    if not projection:
        return set()

    aggregate_re = re.compile(
        rf"^(?:{'|'.join(AGGREGATE_FUNCTIONS)})\s*\((.*)\)$",
        flags=re.IGNORECASE | re.DOTALL,
    )
    simple_identifier_re = re.compile(
        r"^[`\"]?[A-Za-z_][A-Za-z0-9_]*[`\"]?"
        r"(?:\.[`\"]?[A-Za-z_][A-Za-z0-9_]*[`\"]?)*$"
    )

    measurements: set[str] = set()
    for raw_item in _split_top_level_csv(projection):
        item = _strip_alias(raw_item.strip())
        aggregate = aggregate_re.match(item)
        if aggregate:
            item = aggregate.group(1).strip()
        if item == "*" or not simple_identifier_re.fullmatch(item):
            continue
        measurement = _strip_identifier_quotes(item.rsplit(".", 1)[-1])
        if measurement:
            measurements.add(measurement.lower())
    return measurements


def validate_tree_query_shape(sql: str) -> list[dict[str, object]]:
    """Return high-confidence Tree SQL shape issues before runtime execution.

    This intentionally covers only patterns that are almost certainly wrong in
    ordinary Tree reads. It does not try to infer schema or rewrite SQL.
    """
    from_identifier = _extract_from_identifier(sql)
    if not from_identifier.lower().startswith("root.") or "*" in from_identifier:
        return []

    path_segments = [_strip_identifier_quotes(part) for part in from_identifier.split(".") if part]
    if len(path_segments) < 4:
        return []

    from_tail = path_segments[-1].lower()
    projection_measurements = _projection_measurement_names(sql)
    if from_tail not in projection_measurements:
        return []

    device_path = ".".join(path_segments[:-1])
    measurement = path_segments[-1]
    return [
        {
            "code": "tree_dialect_from_must_use_device_path",
            "message": (
                "Tree SQL should use a device/path-pattern in FROM and project "
                "measurement names in SELECT. The FROM path appears to include "
                f"measurement `{measurement}`."
            ),
            "rewrite_hint": (
                f"Use FROM {device_path} and keep `{measurement}` in the SELECT "
                "projection or aggregate expression."
            ),
            "from_path": from_identifier,
            "suggested_from_path": device_path,
            "measurement": measurement,
        }
    ]


def tree_from_wildcard_runtime_hint(sql: str, *, row_count: int | None = None) -> dict[str, object] | None:
    """Return a non-blocking hint for device-path wildcard FROM shapes.

    IoTDB Tree accepts prefix paths such as ``root.sg.d1.*`` syntactically, but
    agents often build that shape by appending ``.*`` to a device path copied
    from schema metadata. For ordinary measurement projections, the canonical
    query shape is ``SELECT measurement FROM root.sg.d1``. This helper is a
    runtime diagnostic only; it does not reject broad scans such as ``root.**``.
    """
    from_identifier = _extract_from_identifier(sql)
    if not from_identifier.lower().startswith("root."):
        return None
    if not from_identifier.endswith(".*"):
        return None
    suggested_from_path = from_identifier[:-2]
    if not suggested_from_path or suggested_from_path.endswith("."):
        return None
    projection_measurements = _projection_measurement_names(sql)
    if not projection_measurements:
        return None

    qualifier = " If this query returned zero rows," if row_count == 0 else ""
    return {
        "code": "tree_dialect_from_path_wildcard_noncanonical",
        "message": (
            f"Tree SQL `FROM {from_identifier}` is a path-pattern form, not the "
            "canonical device-scoped form for ordinary measurement reads."
            f"{qualifier} retry with the device path in FROM and keep measurement names in SELECT."
        ),
        "rewrite_hint": (
            f"Use FROM {suggested_from_path}; metadata `paths`/`timeseries` values are full "
            "timeseries paths and should not be copied as FROM path.* clauses."
        ),
        "from_path": from_identifier,
        "suggested_from_path": suggested_from_path,
    }


def assert_tree_query_shape(sql: str) -> None:
    issues = validate_tree_query_shape(sql)
    if not issues:
        return
    issue = issues[0]
    raise ValueError(
        f"{issue['code']}: {issue['message']} {issue['rewrite_hint']}"
    )
