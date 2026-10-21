"Public API."
import copy


def body_without_request_id(response):
    """Deep-copy the full parsed JSON body, assert ``error.request_id``
    existed as a non-empty string, delete only that key, and return the
    rest of the body untouched for an exact full-body comparison."""
    body = copy.deepcopy(response.json())
    error = body.get("error")
    assert isinstance(error, dict), "expected an {'error': {...}} envelope"
    request_id = error.get("request_id")
    assert isinstance(request_id, str) and request_id, "error.request_id missing/empty"
    del error["request_id"]
    return body
