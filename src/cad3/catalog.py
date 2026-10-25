"""Model-facing standard-parts catalog.

The catalog is deliberately geometry-free.  It contains the portable part
selection contract that a native FreeCAD script can consume: identifiers,
nominal dimensions, interface roles, purchase fields and declared omissions.
FreeCAD geometry belongs to the calling project and is never imported here.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping
import math
from string import Formatter
from .part_contracts import SUPPORTED, resolve

CATALOG_SCHEMA = "cadparts.catalog-family/v1"
INDEX_SCHEMA = "cadparts.catalog-index/v1"
INSTANCE_SCHEMA = "cad3.part-instance/v1"


class CatalogError(Exception):
    """The catalog is missing, malformed, or cannot satisfy a request."""


def catalog_root() -> Path:
    """Return the bundled, read-only catalog data directory."""

    return Path(__file__).resolve().parent / "data" / "catalog"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"cannot read catalog file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CatalogError(f"catalog file {path} must contain an object")
    return value


def _manifest_paths() -> list[Path]:
    root = catalog_root()
    if not root.is_dir():
        raise CatalogError(f"catalog data directory is missing: {root}")
    return sorted(path for path in root.glob("*/*.json") if path.name != "index.json")


def _validate_manifest(data: Mapping[str, Any], path: Path) -> None:
    required = {
        "schema",
        "id",
        "category",
        "name",
        "summary",
        "geometry",
        "sample",
        "parameters",
    }
    missing = sorted(required - set(data))
    if missing:
        raise CatalogError(f"{path}: missing required fields: {', '.join(missing)}")
    if data["schema"] != CATALOG_SCHEMA:
        raise CatalogError(f"{path}: unsupported schema {data['schema']!r}")
    if not isinstance(data["parameters"], list) or not data["parameters"]:
        raise CatalogError(f"{path}: parameters must be a non-empty list")
    if not isinstance(data["geometry"], Mapping) or not data["geometry"].get(
        "fidelity"
    ):
        raise CatalogError(f"{path}: geometry.fidelity is required")
    for interface in data.get("interfaces", []):
        if not isinstance(interface, Mapping) or not {"id", "type", "role"} <= set(
            interface
        ):
            raise CatalogError(f"{path}: every interface needs id, type and role")


def load_manifests() -> list[dict[str, Any]]:
    """Load family declarations in deterministic order."""

    manifests = []
    seen: set[str] = set()
    for path in _manifest_paths():
        data = _read_json(path)
        _validate_manifest(data, path)
        identifier = str(data["id"])
        if identifier in seen:
            raise CatalogError(f"duplicate family id: {identifier}")
        seen.add(identifier)
        data["_source_path"] = str(path.relative_to(catalog_root().parents[3]))
        manifests.append(data)
    return sorted(manifests, key=lambda item: str(item["id"]))


def build_index() -> dict[str, Any]:
    entries = []
    for m in load_manifests():
        common = dict(
            family=m["id"],
            category=m["category"],
            summary=m["summary"],
            keywords=m.get("keywords", []),
            geometry_fidelity=m["geometry"]["fidelity"],
            compatibility=m.get("compatibility", {}),
            path=m["_source_path"],
        )
        entries.append(
            dict(
                common,
                id=m["id"],
                kind="family",
                name=m["name"],
                aliases=m.get("aliases", []),
                selectors=m.get("selectors", {}),
            )
        )
        for item in m.get("items", []):
            entries.append(
                dict(
                    common,
                    id=item["id"],
                    kind="item",
                    name=item.get("name", item["id"]),
                    aliases=list(
                        dict.fromkeys(
                            [item["id"].rsplit(".", 1)[-1], *item.get("aliases", [])]
                        )
                    ),
                    selectors={
                        **m.get("selectors", {}),
                        **item.get("generator", {}).get("params", {}),
                    },
                    dimensions_mm=item.get("dimensions_mm", {}),
                    generator=item.get("generator", {}),
                )
            )
    return dict(schema=INDEX_SCHEMA, entries=sorted(entries, key=lambda x: x["id"]))


def load_catalog_index() -> dict[str, Any]:
    data = _read_json(catalog_root() / "index.json")
    if data.get("schema") != INDEX_SCHEMA:
        raise CatalogError("unsupported catalog index")
    return data


def _capability(entry):
    supported = entry["family"] in SUPPORTED
    reason = None
    if (
        entry["family"] == "linear.bushing.ball"
        and entry.get("generator", {}).get("params", {}).get("style") == "LME"
    ):
        supported = False
        reason = "LME metric/imperial profile dimensions need verification before generating a proxy"
    elif not supported:
        reason = (
            "catalog contract is available; native FreeCAD proxy is not implemented"
        )
    return dict(instance=supported, native_proxy=supported, reason=reason)


def _family_manifest(identifier: str) -> dict[str, Any]:
    wanted = identifier.casefold()
    for manifest in load_manifests():
        if str(manifest["id"]).casefold() == wanted or any(
            str(alias).casefold() == wanted for alias in manifest.get("aliases", [])
        ):
            return manifest
    raise CatalogError(
        f"unknown catalog family {identifier!r}; use catalog search first"
    )


def entry_by_id(identifier: str) -> dict[str, Any]:
    """Resolve a concrete item or family alias without loading geometry."""

    if not isinstance(identifier, str) or not identifier.strip():
        raise CatalogError("catalog identifier must be a non-empty string")
    wanted = identifier.casefold()
    for entry in load_catalog_index()["entries"]:
        candidates = [entry.get("id"), *entry.get("aliases", [])]
        if any(str(candidate).casefold() == wanted for candidate in candidates):
            return deepcopy(entry)
    raise CatalogError(f"unknown catalog part {identifier!r}; use catalog search first")


def _lookup(entry: Mapping[str, Any], key: str) -> Any:
    current: Any = entry
    for component in key.split("."):
        if not isinstance(current, Mapping) or component not in current:
            current = None
            break
        current = current[component]
    if current is not None:
        return current
    for section in ("dimensions_mm", "selectors"):
        values = entry.get(section, {})
        if isinstance(values, Mapping) and key in values:
            return values[key]
    return None


def _matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, Mapping):
        if actual is None:
            return False
        if "eq" in expected and actual != expected["eq"]:
            return False
        if "min" in expected and (
            not isinstance(actual, (int, float)) or actual < expected["min"]
        ):
            return False
        if "max" in expected and (
            not isinstance(actual, (int, float)) or actual > expected["max"]
        ):
            return False
        return True
    if isinstance(actual, (list, tuple)):
        return any(str(item).casefold() == str(expected).casefold() for item in actual)
    if isinstance(actual, str) or isinstance(expected, str):
        return str(actual).casefold() == str(expected).casefold()
    return actual == expected


def _text(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(f"{key} {_text(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return " ".join(_text(item) for item in value)
    return str(value)


def search(
    query: str = "",
    *,
    category: str | None = None,
    constraints: Mapping[str, Any] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Return a small ranked list of family/item contracts."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise CatalogError("limit must be an integer between 1 and 50")
    normalized = str(query or "").strip().casefold()
    terms = [term for term in re.split(r"[\s,，;；/]+", normalized) if term]
    compact = "".join(terms)
    constraints = constraints or {}
    ranked: list[tuple[int, dict[str, Any]]] = []
    for entry in load_catalog_index()["entries"]:
        if (
            category
            and str(entry.get("category", "")).casefold() != category.casefold()
        ):
            continue
        if any(
            not _matches(_lookup(entry, key), value)
            for key, value in constraints.items()
        ):
            continue
        identifiers = [entry.get("id"), *entry.get("aliases", [])]
        normalized_ids = {str(value).casefold() for value in identifiers}
        haystack = _text(entry).casefold()
        score = 1 if not normalized else 0
        if normalized in normalized_ids:
            score += 200
        elif compact and compact in {
            re.sub(r"[\s,，;；/]+", "", value) for value in normalized_ids
        }:
            score += 160
        if normalized and normalized in haystack:
            score += 80
        for term in terms:
            if term in normalized_ids:
                score += 60
            elif term in haystack:
                score += 10
        if normalized and score == 0:
            continue
        result = {
            key: deepcopy(entry[key])
            for key in (
                "id",
                "family",
                "kind",
                "category",
                "name",
                "summary",
                "dimensions_mm",
                "selectors",
                "geometry_fidelity",
                "compatibility",
                "path",
            )
            if key in entry
        }
        result["score"] = score
        result["capabilities"] = _capability(entry)
        ranked.append((score, result))
    ranked.sort(key=lambda item: (-item[0], str(item[1]["id"])))
    return [item for _, item in ranked[:limit]]


def _family_contract(manifest: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(manifest))
    result.pop("items", None)
    result.pop("_source_path", None)
    return result


def describe(identifier: str) -> dict[str, Any]:
    """Return one family or item contract with progressive disclosure."""

    entry = entry_by_id(identifier)
    family = _family_manifest(str(entry["family"]))
    if entry.get("kind") == "family":
        return {"family": _family_contract(family), "capabilities": _capability(entry)}
    return {
        "entry": entry,
        "capabilities": _capability(entry),
        "family": _family_contract(family),
        "generator": deepcopy(entry.get("generator", {})),
    }


def compare(*identifiers: str) -> dict[str, Any]:
    """Return only selection-relevant fields for two or more candidates."""

    if len(identifiers) < 2:
        raise CatalogError("compare requires at least two catalog identifiers")
    candidates = []
    for identifier in identifiers:
        entry = entry_by_id(identifier)
        candidates.append(
            {
                key: deepcopy(entry[key])
                for key in (
                    "id",
                    "family",
                    "kind",
                    "category",
                    "name",
                    "summary",
                    "dimensions_mm",
                    "selectors",
                    "geometry_fidelity",
                    "compatibility",
                    "path",
                )
                if key in entry
            }
        )
    return {"schema": "cad3.catalog-comparison/v1", "candidates": candidates}


def _parameters(family, pinned, overrides):
    declarations = {v["name"]: v for v in family["parameters"]}
    unknown = set(overrides) - set(declarations)
    if unknown:
        raise CatalogError(f"unknown parameters: {sorted(unknown)}")
    result = dict(pinned)
    for key, value in overrides.items():
        if key in pinned and value != pinned[key]:
            raise CatalogError(
                f"catalog item pins {key}={pinned[key]!r}; select another item"
            )
        result[key] = value
    for key, rule in declarations.items():
        if key not in result:
            if rule["required"]:
                raise CatalogError(f"missing parameter {key}; see catalog describe")
            result[key] = rule["default"]
        value = result[key]
        types = rule["type"].split("|")
        if (
            "string" in types
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            value = str(value)
            result[key] = value
        if value is None and "null" in types:
            continue
        valid = (
            ("string" in types and isinstance(value, str))
            or (
                "number" in types
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            )
            or (
                "integer" in types
                and isinstance(value, int)
                and not isinstance(value, bool)
            )
            or ("boolean" in types and isinstance(value, bool))
        )
        if not valid:
            raise CatalogError(f"{key} must be {rule['type']}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise CatalogError(f"{key} must be finite")
            if "minimum" in rule and value < rule["minimum"]:
                raise CatalogError(f"{key} below minimum")
        if rule.get("choices") and value not in rule["choices"]:
            raise CatalogError(f"{key} choices: {rule['choices']}")
    return result


def instantiate(
    identifier: str,
    *,
    selections: Mapping[str, Any] | None = None,
    parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve parameters to reusable local ports and a native proxy contract."""
    entry = entry_by_id(identifier)
    family = _family_manifest(entry["family"])
    capability = _capability(entry)
    if not capability["instance"]:
        raise CatalogError(capability["reason"] or f"{entry['family']} is catalog-only")
    p = _parameters(
        family, entry.get("generator", {}).get("params", {}), dict(parameters or {})
    )
    aliases = family.get("alias_selections", {})
    selected = next(
        (dict(v) for k, v in aliases.items() if k.casefold() == identifier.casefold()),
        {},
    )
    for key, value in (selections or {}).items():
        if key in selected and value != selected[key]:
            raise CatalogError(f"alias pins selection {key}={selected[key]}")
        selected[key] = value
    for key, value in selected.items():
        rule = family.get("selectors", {}).get(key)
        if rule is None or key in p:
            raise CatalogError(
                f"unknown purchase selection {key}; use parameters for geometry"
            )
        if isinstance(rule, list) and value not in rule:
            raise CatalogError(f"invalid selection {key}={value}; choices {rule}")
    if entry["family"] == "bearing.deep_groove":
        if p["detail"] != "simplified":
            raise CatalogError("native bearing proxy supports detail=simplified only")
        match = entry_by_id(str(p["code"]))
        if match["family"] != entry["family"]:
            raise CatalogError("invalid bearing code")
        dimensions = match["dimensions_mm"]
    else:
        dimensions = entry.get("dimensions_mm", {})
    try:
        d, ports = resolve(entry["family"], p, dimensions)
    except (ValueError, KeyError) as exc:
        raise CatalogError(f"{entry['family']}: {exc}") from exc
    purchase = deepcopy(family.get("purchase", {}))
    values = {**p, **d, **selected}
    template = purchase.get("designation_template", "")
    if all(k in values for _, k, _, _ in Formatter().parse(template) if k):
        purchase["query"] = template.format(**values)
    if entry["family"] == "bearing.deep_groove":
        code = p["code"]
        for key, skip in [("closure", "open"), ("clearance", "normal")]:
            if selected.get(key) and selected[key] != skip:
                code += "-" + selected[key]
        purchase.update(order_code=code, query=code + " deep-groove bearing 深沟球轴承")
    elif entry["family"] == "motor.stepper.square":
        purchase["query"] = (
            f"stepper motor {d['face']} mm frame shaft {d['shaft']} mm body {d['body_length']} mm"
        )
    elif entry["family"] == "linear.bushing.ball":
        purchase["query"] = (
            f"{p['style']}{p['bore']}UU linear ball bushing"
            if selected.get("seal") == "UU"
            else f"{p['style']}{p['bore']} linear ball bushing"
        )
    source = _read_json(catalog_root().parent / "catalog-source.json")
    return dict(
        schema=INSTANCE_SCHEMA,
        library="cad3-standard-parts",
        library_version=source["catalog_version"],
        catalog_id=entry["id"],
        family=entry["family"],
        parameters=p,
        selection=selected,
        dimensions_mm=d,
        ports=ports,
        interfaces=ports,
        purchase=purchase,
        geometry=deepcopy(family["geometry"]),
        compatibility=deepcopy(family.get("compatibility", {})),
        source=family["_source_path"],
        coordinate_system="local millimetres; +Z longitudinal axis",
        geometry_mode="fixed standard proxy; Placement remains editable; rerun to change a catalog selection",
    )


__all__ = [
    "CatalogError",
    "search",
    "describe",
    "compare",
    "instantiate",
    "load_catalog_index",
    "build_index",
]
