"""Derives activity type, value classification, and periodicity from task names."""

CONTROL_KEYWORDS = ("check", "verify", "validate", "inspect", "review", "audit", "confirm")
AUTHORIZE_KEYWORDS = ("approve", "authorize", "sign off", "sign-off", "accept", "reject")
COMMUNICATION_KEYWORDS = ("send", "notify", "inform", "contact", "call", "email", "receive", "request", "reply")
BATCH_KEYWORDS = ("batch", "consolidate", "aggregate", "compile")
PERIODIC_KEYWORDS = ("end of day", "eod", "daily", "weekly", "monthly", "periodic", "end of month")
VA_KEYWORDS = ("create", "prepare", "produce", "build", "design", "develop", "generate")


def classify_activity_type(name: str) -> str:
    lower = name.lower()
    if any(k in lower for k in COMMUNICATION_KEYWORDS):
        return "communication"
    if any(k in lower for k in CONTROL_KEYWORDS):
        return "check"
    if any(k in lower for k in AUTHORIZE_KEYWORDS):
        return "authorize"
    if any(k in lower for k in BATCH_KEYWORDS):
        return "batch"
    return "basic"


def classify_value(name: str, activity_type: str) -> str:
    lower = name.lower()
    if activity_type in ("check", "authorize"):
        return "BVA"
    if any(k in lower for k in VA_KEYWORDS):
        return "VA"
    return "BVA"


def is_periodic(name: str) -> bool:
    lower = name.lower()
    return any(k in lower for k in PERIODIC_KEYWORDS)


def is_batch(name: str, activity_type: str) -> bool:
    return activity_type == "batch"


def derive(name: str) -> dict:
    activity_type = classify_activity_type(name)
    return {
        "activity_type": activity_type,
        "value_classification": classify_value(name, activity_type),
        "is_periodic": is_periodic(name),
        "is_batch": is_batch(name, activity_type),
    }
