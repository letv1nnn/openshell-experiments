import logging

import yaml

log = logging.getLogger("config")


def load_config(path: str) -> dict:
    with open(path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"config.yaml is not a YAML mapping: {path}")
    repos = raw.get("repos")
    if not repos:
        raise ValueError("config.yaml has no repos[] entries.")
    for i, r in enumerate(repos):
        if not r.get("org") or not r.get("repo"):
            raise ValueError(f"repos[{i}] is missing 'org' or 'repo'.")
    return raw
