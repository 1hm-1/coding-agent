from importlib import import_module


def route_error(error_code: str) -> int:
    for index in range(1, 13):
        plugin = import_module(f"error_router.plugins.plugin_{index:02d}")
        if plugin.ERROR_CODE == error_code:
            return plugin.STATUS
    return 500
