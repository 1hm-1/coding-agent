def apply(payload):
    if "legacy_customer_id" not in payload:
        return payload
    updated = dict(payload)
    updated["account_id"] = updated.pop("legacy_customer_id")
    return updated
