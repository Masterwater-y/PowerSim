import json
from pathlib import Path


class ModelError(ValueError):
    pass


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(obj, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=False)
        f.write("\n")


def load_model(path):
    model = load_json(path)
    validate_model(model, str(path))
    return model


def validate_model(model, path="<model>"):
    if not isinstance(model, dict):
        raise ModelError(f"{path}: model must be a JSON object")
    for key in ("name", "counters", "rules"):
        if key not in model:
            raise ModelError(f"{path}: missing required key {key!r}")
    counters = model["counters"]
    if not isinstance(counters, list) or not counters:
        raise ModelError(f"{path}: counters must be a non-empty list")
    names = set()
    for c in counters:
        if not isinstance(c, dict) or not c.get("name"):
            raise ModelError(f"{path}: each counter needs a name")
        if c["name"] in names:
            raise ModelError(f"{path}: duplicate counter {c['name']}")
        names.add(c["name"])
    rules = model["rules"]
    if not isinstance(rules, list) or not rules:
        raise ModelError(f"{path}: rules must be a non-empty list")
    for r in rules:
        if not r.get("name") or not isinstance(r.get("signature"), dict):
            raise ModelError(f"{path}: each rule needs name and signature")
        for counter, value in r["signature"].items():
            if counter not in names:
                raise ModelError(f"{path}: rule {r['name']} references unknown counter {counter}")
            _validate_signature_value(value, path, r["name"], counter)
    return True


def _validate_signature_value(value, path, rule, counter):
    if isinstance(value, (int, float)):
        return
    if isinstance(value, dict) and "min" in value and "max" in value:
        lo = float(value["min"])
        hi = float(value["max"])
        if lo > hi:
            raise ModelError(f"{path}: rule {rule} counter {counter} has min > max")
        return
    raise ModelError(f"{path}: rule {rule} counter {counter} must be number or {{min,max}}")


def counter_names(model):
    return [c["name"] for c in model.get("counters", [])]


def counter_component_hints(model):
    hints = {}
    for c in model.get("counters", []):
        if c.get("component_hint"):
            hints[c["name"]] = c["component_hint"]
    return hints

