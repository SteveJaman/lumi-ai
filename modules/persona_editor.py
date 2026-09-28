"""
Persona YAML round-trip editor.

Preferred backend: ruamel.yaml — preserves comments, key order, and
quoting style through a load → edit → save cycle.

Fallback backend: stdlib PyYAML. Functionally identical, but comments
in lumi.yaml will be lost on the first save. A warning is surfaced to
the caller so the UI can tell the user to `pip install ruamel.yaml`.

Public API is backend-agnostic:

    load_raw_text(path) -> str
    load_parsed(path)   -> parsed structure (CommentedMap or dict)
    parsed_to_plain(d)  -> plain dict/list/scalar tree
    merge_into(cur, inc) -> merged structure (in-place for ruamel)
    save_parsed(path, d) -> writes file
    dump_to_string(d)   -> str
    backend_name()      -> "ruamel" | "pyyaml"
    backend_preserves_comments() -> bool
"""

import io

# ---- Backend selection ---------------------------------------------
_HAS_RUAMEL = False
try:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    from ruamel.yaml.scalarstring import (
        FoldedScalarString, LiteralScalarString,
    )
    _HAS_RUAMEL = True
except ImportError:
    import yaml as _std_yaml


def backend_name():
    return "ruamel" if _HAS_RUAMEL else "pyyaml"


def backend_preserves_comments():
    return _HAS_RUAMEL


# ---- ruamel path ---------------------------------------------------
if _HAS_RUAMEL:
    _yaml = YAML()
    _yaml.preserve_quotes = True
    _yaml.indent(mapping=2, sequence=4, offset=2)
    _yaml.width = 4096   # don't wrap long lines

    def _load_stream(stream):
        return _yaml.load(stream)

    def _dump_stream(data, stream):
        _yaml.dump(data, stream)

    def _empty_map():
        return CommentedMap()

    def _empty_seq():
        return CommentedSeq()

    def _to_backend(value):
        if isinstance(value, (FoldedScalarString, LiteralScalarString)):
            return value
        if isinstance(value, dict):
            m = CommentedMap()
            for k, v in value.items():
                m[k] = _to_backend(v)
            return m
        if isinstance(value, list):
            s = CommentedSeq()
            for v in value:
                s.append(_to_backend(v))
            return s
        return value

    def _preserve_style(old, new):
        if not isinstance(new, str):
            return new
        if isinstance(old, FoldedScalarString):
            return FoldedScalarString(new)
        if isinstance(old, LiteralScalarString):
            return LiteralScalarString(new)
        return new

# ---- PyYAML fallback path -----------------------------------------
else:
    # Enable Unicode output in the fallback dumper — stdlib yaml
    # defaults to escaped \uXXXX which makes lumi.yaml unreadable.
    def _load_stream(stream):
        return _std_yaml.safe_load(stream)

    def _dump_stream(data, stream):
        _std_yaml.safe_dump(
            data, stream,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=4096,
        )

    def _empty_map():
        return {}

    def _empty_seq():
        return []

    def _to_backend(value):
        # Plain Python types are fine for PyYAML.
        if isinstance(value, dict):
            return {k: _to_backend(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_to_backend(v) for v in value]
        return value

    def _preserve_style(old, new):
        return new


# ---- Public API ----------------------------------------------------

def load_raw_text(path):
    """Return the raw YAML text of the file (comments and all)."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_parsed(path):
    """Parse the file into a backend-native structure."""
    with open(path, "r", encoding="utf-8") as f:
        return _load_stream(f)


def parsed_to_plain(data):
    """Convert a backend-native structure to plain Python types so it
    can be json.dumps'd."""
    if isinstance(data, dict):
        return {k: parsed_to_plain(v) for k, v in data.items()}
    if isinstance(data, list):
        return [parsed_to_plain(v) for v in data]
    return data


def merge_into(current, incoming):
    """Recursively apply `incoming` (plain dict) onto `current`
    (backend structure). Keys present in both are merged if dicts,
    replaced otherwise. Keys only in current are deleted. Keys only
    in incoming are added.

    For ruamel this preserves comments on the survivors. For PyYAML
    it's a straight dict overwrite.
    """
    if not isinstance(incoming, dict):
        return incoming

    if not isinstance(current, dict):
        current = _empty_map()

    # Delete keys no longer present.
    for key in list(current.keys()):
        if key not in incoming:
            del current[key]

    for key, value in incoming.items():
        if key in current:
            cur_val = current[key]
            if isinstance(cur_val, dict) and isinstance(value, dict):
                merge_into(cur_val, value)
                continue
            value = _preserve_style(cur_val, value)
        current[key] = _to_backend(value)

    return current


def save_parsed(path, data):
    """Write the backend structure to disk."""
    with open(path, "w", encoding="utf-8") as f:
        _dump_stream(data, f)


def dump_to_string(data):
    buf = io.StringIO()
    _dump_stream(data, buf)
    return buf.getvalue()