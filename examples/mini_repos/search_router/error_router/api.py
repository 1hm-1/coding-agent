from error_router.plugins import route_error


def response_status(error_code: str) -> int:
    return route_error(error_code)
