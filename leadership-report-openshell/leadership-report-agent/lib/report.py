"""Report validation and normalization."""

from .errors import AgentError


def validate_report(report: dict) -> None:
    """Validate report structure and auto-fix minor formatting issues.

    Raises AgentError if required fields are missing or bullets are malformed.
    """
    for field in ("date", "summary", "bullets"):
        if field not in report or not report[field]:
            raise AgentError(f"Report missing '{field}' field.", retriable=False)

    if not isinstance(report["bullets"], list):
        raise AgentError("'bullets' must be a list.", retriable=False)

    for i, b in enumerate(report["bullets"]):
        if "label" not in b or "text" not in b:
            raise AgentError(f"Bullet {i} missing 'label' or 'text'.", retriable=False)
        if not b["label"].endswith(":"):
            b["label"] = b["label"].rstrip() + ":"
        if not b["text"].startswith(" "):
            b["text"] = " " + b["text"]
        if not b["text"].endswith("\n"):
            b["text"] = b["text"].rstrip() + "\n"
