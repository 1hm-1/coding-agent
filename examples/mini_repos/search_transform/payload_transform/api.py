from importlib import import_module


def normalize_payload(payload: dict[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    for index in range(1, 13):
        transform = import_module(f"payload_transform.transforms.transform_{index:02d}")
        normalized = transform.apply(normalized)
    return normalized
