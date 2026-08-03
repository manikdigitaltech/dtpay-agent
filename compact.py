"""
Compact tabular encoding for the daily/hourly breakdowns sent to
Claude. A list of dicts like

    [{"date": "2026-07-24", "total_resolved": 5105, "completed": 160,
      "conversion_rate_pct": 3.13}, ...]

repeats every field name on every row when serialized as JSON - for a
2-day request's hourly breakdown, that's the same 4 field names
repeated 48 times, plus a brace/quote/colon/comma for each one. The
values and their meaning are identical whether the field names are
stated once or 48 times, so restating them isn't buying anything.

to_compact_table() turns the same rows into one header line naming
the columns, then one line per row with just the values,
comma-separated - a plain-text table Claude reads exactly as well as
the equivalent JSON, at a fraction of the tokens for anything with
more than a couple of rows. Only used for daily/hourly data
specifically (dates, hours, counts, rates) - never for reason codes
or free text, since a value containing a literal comma would make a
naive CSV split ambiguous, and daily/hourly's columns are all
numbers or ISO date/hour strings that can't contain one.

from_compact_table() is the exact inverse, used only by this module's
own tests to prove the transformation loses nothing - never called
from application code.
"""


def to_compact_table(rows, columns):
    """
    rows: list of dicts, all containing every key in columns.
    columns: the keys to include, in the order they should appear.
    Returns a CSV-style string: header line, then one line per row.
    Returns "(none)" for an empty list, since an empty string alone
    would be easy to misread as missing data instead of a genuine
    empty result.
    """
    if not rows:
        return "(none)"
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(str(row[c]) for c in columns))
    return "\n".join(lines)


def from_compact_table(table, columns):
    """
    Exact inverse of to_compact_table, for verifying no information
    is lost - not used by any application code, only by tests. Parses
    numeric-looking values back to int/float so a round-trip
    comparison against the original rows (which have real int/float
    values, not strings) can use plain equality.
    """
    if table == "(none)":
        return []
    lines = table.split("\n")
    header = lines[0].split(",")
    assert header == columns, f"column mismatch: {header} != {columns}"
    rows = []
    for line in lines[1:]:
        values = line.split(",")
        row = {}
        for col, val in zip(columns, values):
            if val.replace(".", "", 1).replace("-", "", 1).isdigit():
                row[col] = float(val) if "." in val else int(val)
            else:
                row[col] = val
        rows.append(row)
    return rows