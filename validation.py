import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, RefResolver
from jsonschema.exceptions import ValidationError


class InvalidRequest(ValueError):
    """Raised when request data violates the OpenAPI contract."""


class ConfigurationError(RuntimeError):
    """Raised when the implementation cannot find its OpenAPI operation."""


# Load the OpenAPI contract once
_CONTRACT_PATH = Path(__file__).with_name("contracts") / "encore-openapi.yaml"
with _CONTRACT_PATH.open(encoding="utf-8") as contract_file:
    _CONTRACT = yaml.safe_load(contract_file)

# Map Flask routes and parameter names to OpenAPI
_FLASK_ROUTES = {
    "/events": "/events",
    "/events/<event_id>": "/events/{eventId}",
    "/events/<event_id>/reservations": "/events/{eventId}/reservations",
    "/reservations/<res_id>": "/reservations/{reservationId}",
    "/events/<event_id>/waitlist": "/events/{eventId}/waitlist",
    "/events/<event_id>/waitlist/<entry_id>": "/events/{eventId}/waitlist/{entryId}",
}
_PARAMETER_NAMES = {
    "event_id": "eventId",
    "res_id": "reservationId",
    "entry_id": "entryId",
}


def _operation_for(method, route):
    path = _FLASK_ROUTES.get(route, route)
    path_item = _CONTRACT.get("paths", {}).get(path)
    if path_item is None or method.lower() not in path_item:
        raise ConfigurationError(
            f"No OpenAPI operation for {method.upper()} {route}"
        )
    return path, path_item[method.lower()]


def _schema_validator(schema):
    if not isinstance(schema, dict):
        raise ConfigurationError("OpenAPI schema must be an object")
    # Resolve schema references using the OpenAPI contract
    resolver = RefResolver.from_schema(_CONTRACT)
    return Draft202012Validator(schema, resolver=resolver)


def _validate_value(schema, value):
    try:
        _schema_validator(schema).validate(value)
    except ValidationError as error:
        raise InvalidRequest from error


def _path_parameters(operation, path_params):
    # Convert Flask parameter names to OpenAPI names
    return {
        _PARAMETER_NAMES.get(name, name): value
        for name, value in (path_params or {}).items()
    }


def _validate_parameters(operation, path_params, query_params):
    path_params = _path_parameters(operation, path_params)
    query_params = query_params or {}

    # Check request parameters against their schemas
    for parameter in operation.get("parameters", []):
        if "$ref" in parameter:
            raise ConfigurationError("Parameter-level $ref is not supported")

        name = parameter["name"]
        location = parameter["in"]
        if location == "path":
            value = path_params.get(name)
        elif location == "query":
            value = query_params.get(name)
        else:
            continue

        if value is None:
            if parameter.get("required"):
                raise InvalidRequest
            continue

        # Convert query strings to booleans for validation
        if location == "query" and parameter["schema"].get("type") == "boolean":
            if value == "true":
                value = True
            elif value == "false":
                value = False
            else:
                raise InvalidRequest

        _validate_value(parameter["schema"], value)


def validate_request(
    method,
    route,
    path_params=None,
    query_params=None,
    body_bytes=None,
    content_type=None,
):
    """Validate supported request data against one OpenAPI operation.

    ``route`` may be an OpenAPI path template or one of the Flask route rules
    in this module's explicit mapping. The returned value is the decoded JSON
    body, or ``None`` when the operation has no body.
    """
    _, operation = _operation_for(method, route)
    _validate_parameters(operation, path_params, query_params)

    # Check the request body's requirements
    request_body = operation.get("requestBody")
    if request_body is None:
        return None

    body_bytes = b"" if body_bytes is None else body_bytes
    if not body_bytes:
        if request_body.get("required"):
            raise InvalidRequest
        return None

    if content_type != "application/json":
        raise InvalidRequest

    try:
        # Check if the request body contains valid JSON
        body = json.loads(body_bytes)
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise InvalidRequest from error

    schema = request_body["content"]["application/json"]["schema"]
    _validate_value(schema, body)
    return body
