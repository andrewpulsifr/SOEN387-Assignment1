import time

from flask import Blueprint, jsonify, request

import hold_services
import services
import validation

api_routes = Blueprint("api", __name__)


@api_routes.before_request
def validate_get_request():
    if request.method != "GET":
        return None

    try:
        validation.validate_request(
            request.method,
            request.url_rule.rule,
            request.view_args,
            request.args,
        )
    except validation.InvalidRequest:
        return jsonify({"error": "invalid_request"}), 422
    return None


@api_routes.get("/events")
def list_events():
    body, status = services.list_events(request.args.get("bookable"))
    return jsonify(body), status


@api_routes.get("/events/<event_id>")
def get_event(event_id):
    body, status = services.get_event(event_id)
    return jsonify(body), status


# Listing 3.1 -- Encore v1.1: reservations with an idempotency-key mechanism.
# The contract's new promise: a repeated POST with the same Idempotency-Key
# is recognized as a repeat and answered with the SAME outcome, applied once.
@api_routes.post("/events/<event_id>/reservations")
def create_reservation(event_id):
    key = request.headers.get("Idempotency-Key")
    body_bytes = request.get_data() if key else b""
    body, status, replayed = services.create_reservation(
        event_id, key, body_bytes, request.mimetype
    )

    if not replayed and status in (201, 404, 409):
        if request.headers.get("X-Demo-Lose-Reply") == "true":
            time.sleep(3)      # the work is DONE; only the reply is delayed past
                               # the client's timeout -- Chapter 1's third state.

    return _keyed_response(body, status, replayed)


def _keyed_response(body, status, replayed):
    """Build a keyed POST response; a replay carries Idempotency-Replayed: true."""
    response = jsonify(body)
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return response, status


@api_routes.get("/reservations/<res_id>")
def get_reservation(res_id):
    body, status = services.get_reservation(res_id)
    return jsonify(body), status


@api_routes.post("/events/<event_id>/waitlist")
def join_waitlist(event_id):
    key = request.headers.get("Idempotency-Key")
    body_bytes = request.get_data() if key else b""
    body, status, replayed = services.join_waitlist(
        event_id, key, body_bytes, request.mimetype
    )
    return _keyed_response(body, status, replayed)

@api_routes.get("/events/<event_id>/waitlist")
def get_waitlist_entries(event_id):
    body, status = services.get_waitlist_entries(event_id)
    return jsonify(body), status


@api_routes.get("/events/<event_id>/waitlist/<entry_id>")
def get_waitlist_entry_by_id(event_id, entry_id):
    body, status = services.get_waitlist_entry_by_id(event_id, entry_id)
    return jsonify(body), status

@api_routes.delete("/events/<event_id>/waitlist/<entry_id>")
def leave_waitlist(event_id, entry_id):
    body, status = services.leave_waitlist(event_id, entry_id)
    if status == 204:
        return "", status
    return jsonify(body), status


@api_routes.post("/events/<event_id>/holds")
def create_hold(event_id):
    """Hold one seat; without a key the body is ignored and nothing is stored."""
    key = request.headers.get("Idempotency-Key")
    body_bytes = request.get_data() if key else b""
    body, status, replayed = hold_services.create_hold(
        event_id, key, body_bytes, request.mimetype, request.args
    )
    return _keyed_response(body, status, replayed)


@api_routes.get("/holds/<hold_id>")
def get_hold(hold_id):
    """Show a Hold; the service rejects any query or body, as the contract requires."""
    body, status = hold_services.get_hold(hold_id, request.args, request.get_data())
    return jsonify(body), status
