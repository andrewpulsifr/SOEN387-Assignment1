# Listing 2.3 -- A conformance suite written against the CONTRACT, not the code.
# It reads contracts/encore-openapi.yaml, extracts the response schemas, and checks a
# running implementation against them. It never imports the server's source.
# Run from the assignment root (with a server on :3000): python3 tests/conformance.py
import json, sys, urllib.request, urllib.error
from pathlib import Path
import yaml
from jsonschema import Draft202012Validator
from uuid import uuid4

BASE = "http://localhost:3000"
CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "encore-openapi.yaml"
spec = yaml.safe_load(CONTRACT.read_text())

# ---------------Shared helpers -----------------------
def schema_for(path, method, status):
    """Pull a response schema out of the OpenAPI document, and carry the
    components along so that $ref: '#/components/schemas/...' resolves."""
    op = spec["paths"][path][method]
    schema = op["responses"][status]["content"]["application/json"]["schema"]
    return {**schema, "components": spec["components"]}

def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as res:
            return res.status, json.load(res)
    except urllib.error.HTTPError as err:
        return err.code, json.load(err)

def post(path, body, key=None):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Idempotency-Key"] = key

    request = urllib.request.Request(
        BASE + path,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)

def delete(path):
    request = urllib.request.Request(BASE + path, method="DELETE")

    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code

CHECKS = FAILED = 0
def check(name, fn):
    global CHECKS, FAILED
    CHECKS += 1
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:
        FAILED += 1
        msg = str(e).splitlines()[0][:100] if str(e) else type(e).__name__
        print(f"  FAIL  {name}\n        {msg}")

# ---------------Base event checks -----------------------
def t_list_shape():
    status, body = get("/events")
    assert status == 200, f"expected 200, got {status}"
    Draft202012Validator(schema_for("/events", "get", "200")).validate(body)

def t_bookable_filter():
    status, body = get("/events?bookable=true")
    assert status == 200, f"expected 200, got {status}"
    Draft202012Validator(schema_for("/events", "get", "200")).validate(body)
    assert all(e["seatsLeft"] > 0 for e in body["events"]), \
        "bookable=true returned a full event"

def t_get_one():
    status, body = get("/events/ev_101")
    assert status == 200, f"expected 200, got {status}"
    Draft202012Validator(schema_for("/events/{eventId}", "get", "200")).validate(body)

def t_unknown_id():
    status, body = get("/events/ev_999")
    assert status == 404, f"expected 404, got {status}"
    Draft202012Validator(schema_for("/events/{eventId}", "get", "404")).validate(body)

# ---------------Required waitlist checks -----------------------

# 1. Successful join
def t_successful_join():

    # Create a unique user for the waitlist request
    event_id = "ev_102"
    payload = {"userId": f"test_{uuid4().hex}"}

    # Create a POST request with a unique idempotency key
    # Send the request and capture the response, including errors
    status, body = post(
        f"/events/{event_id}/waitlist",
        json.dumps(payload).encode("utf-8"),
        uuid4().hex,
    )

    # Verify the request successfully created a waitlist entry
    assert status == 201, f"expected 201, got {status}: {body}"

    # Validate the response against the OpenAPI contract
    schema = schema_for("/events/{eventId}/waitlist", "post", "201")
    Draft202012Validator(
        schema,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(body)

    # Verify the created entry belongs to the requested event
    assert body["eventId"] == event_id, \
        f"expected {event_id}, got {body['eventId']}"

# 2. Successful replay without duplication
def t_successful_replay():

    event_id = "ev_102"
    path = f"/events/{event_id}/waitlist"

    # Get the initial waitlist for comparison
    status, initial_waitlist = get(path)
    assert status == 200, f"expected 200, got {status}"

    body_bytes = json.dumps({"userId": f"test_{uuid4().hex}"}).encode("utf-8")
    key = uuid4().hex

    # Prepare the POST response validator
    post_schema = schema_for("/events/{eventId}/waitlist", "post", "201")
    validator = Draft202012Validator(
        post_schema,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )

    # Send the same request twice and validate both responses
    responses = []
    for attempt in (1, 2):
        # Send the request and extract the HTTP status and JSON response
        # Capture error responses instead of stopping the test
        status, body = post(path, body_bytes, key)

        # Verify each attempt succeeds and matches the OpenAPI response schema
        assert status == 201, \
            f"attempt {attempt}: expected 201, got {status}: {body}"
        validator.validate(body)
        assert body["eventId"] == event_id

        # Save both responses so they can be compared for equality
        responses.append(body)

    # The replay must return the original response
    assert responses[1] == responses[0], \
        "replay did not return the original entry"

    status, updated_waitlist = get(path)
    assert status == 200, f"expected 200, got {status}"

    Draft202012Validator(
        schema_for("/events/{eventId}/waitlist", "get", "200")
    ).validate(updated_waitlist)

    assert len(updated_waitlist["entries"]) == len(initial_waitlist["entries"]) + 1, \
        "two identical requests must create exactly one entry"

    entry_id = responses[0]["id"]
    assert sum(entry["id"] == entry_id for entry in updated_waitlist["entries"]) == 1, \
        "the created entry must appear exactly once"


# 3. Event with available seats
def t_event_with_available_seats():

    event_id = "ev_101"

    status, event = get(f"/events/{event_id}")
    assert status == 200, f"expected 200, got {status}"
    assert event["seatsLeft"] > 0, "test requires an event with available seats"

    status, body = post(
        f"/events/{event_id}/waitlist",
        json.dumps({"userId": f"test_{uuid4().hex}"}).encode("utf-8"),
        uuid4().hex,
    )

    assert status == 409, f"expected 409, got {status}: {body}"

    schema = schema_for("/events/{eventId}/waitlist", "post", "409")
    Draft202012Validator(schema).validate(body)

    assert body["error"] == "event_not_full", \
        f"expected event_not_full, got {body['error']}"

# 4. Unknown entry
def t_unknown_entry():

    entry_id = f"wl_{uuid4().hex}"
    status, body = get(f"/events/ev_102/waitlist/{entry_id}")

    assert status == 404, f"expected 404, got {status}: {body}"

    schema = schema_for(
        "/events/{eventId}/waitlist/{entryId}", "get", "404"
    )
    Draft202012Validator(schema).validate(body)

    assert body["error"] == "entry_not_found", \
        f"expected entry_not_found, got {body['error']}"

# ---------------Additional waitlist checks -----------------------

# 5. Idempotency and error replay + Different-body key reuse
def t_different_body_key_reuse():
    # Reuse the same idempotency key for both requests
    idempotency_key = uuid4().hex
    path = "/events/ev_102/waitlist"
    contract_path = "/events/{eventId}/waitlist"

    # First request should succeed (201), second should reject key reuse (422)
    for expected_status in (201, 422):

        # Generate a different user for each request, but keep the same key
        payload = {"userId": f"test_{uuid4().hex}"}

        # Send the request and capture both successful and error responses
        status, body = post(
            path, json.dumps(payload).encode("utf-8"), idempotency_key
        )

        assert status == expected_status, \
            f"expected {expected_status}, got {status}: {body}"

        schema = schema_for(contract_path, "post", str(expected_status))
        Draft202012Validator(schema).validate(body)

        if expected_status == 422:
            assert body["error"] == "idempotency_key_reuse", \
                f"expected idempotency_key_reuse, got {body['error']}"


# 6. Missing idempotency key

def t_missing_idempotency_key():

    status, body = post(
        "/events/ev_102/waitlist",
        json.dumps({"userId": f"test_{uuid4().hex}"}).encode("utf-8"),
        key=None,
    )

    assert status == 422, f"expected 422, got {status}: {body}"

    schema = schema_for("/events/{eventId}/waitlist", "post", "422")
    Draft202012Validator(schema).validate(body)

    assert body["error"] == "idempotency_key_required", \
        f"expected idempotency_key_required, got {body['error']}"

# 7. Unknown event
def t_unknown_event():

    status, body = post(
        "/events/ev_999/waitlist",
        json.dumps({"userId": f"test_{uuid4().hex}"}).encode("utf-8"),
        uuid4().hex,
    )

    assert status == 404, f"expected 404, got {status}: {body}"

    schema = schema_for("/events/{eventId}/waitlist", "post", "404")
    Draft202012Validator(schema).validate(body)

    assert body["error"] == "event_not_found", \
        f"expected event_not_found, got {body['error']}"

# 8. Stored already_on_waitlist error
def t_stored_already_on_waitlist_error():

    path = "/events/ev_102/waitlist"
    contract_path = "/events/{eventId}/waitlist"
    payload = json.dumps({"userId": f"test_{uuid4().hex}"}).encode("utf-8")

    # 1. Successfully join the waitlist
    status, entry = post(path, payload, uuid4().hex)
    assert status == 201, f"expected 201, got {status}: {entry}"

    schema = schema_for(contract_path, "post", "201")
    Draft202012Validator(
        schema,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(entry)

    # 2. Try joining again with a new key and store the error
    rejected_key = uuid4().hex
    status, original_error = post(path, payload, rejected_key)
    assert status == 409, f"expected 409, got {status}: {original_error}"

    error_validator = Draft202012Validator(
        schema_for(contract_path, "post", "409")
    )
    error_validator.validate(original_error)

    assert original_error["error"] == "already_on_waitlist", \
        f"expected already_on_waitlist, got {original_error['error']}"

    # 3. Remove the original waitlist entry
    status = delete(f"{path}/{entry['id']}")
    assert status == 204, f"expected 204, got {status}"

    # 4. Retry the rejected request and verify the stored error
    status, replayed_error = post(path, payload, rejected_key)
    assert status == 409, f"expected 409, got {status}: {replayed_error}"

    error_validator.validate(replayed_error)

    assert replayed_error == original_error, \
        "retry did not replay the stored already_on_waitlist error"

# ---------------Waitlist lifecycle -----------------------
# 9. Listing, retrieval, deletion, and repositioning
def t_waitlist_lifecycle():

    event_id = "ev_102"
    path = f"/events/{event_id}/waitlist"
    contract_path = "/events/{eventId}/waitlist"
    entry_contract_path = contract_path + "/{entryId}"

    list_validator = Draft202012Validator(
        schema_for(contract_path, "get", "200"),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    entry_validator = Draft202012Validator(
        schema_for(entry_contract_path, "get", "200"),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    post_validator = Draft202012Validator(
        schema_for(contract_path, "post", "201"),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )

    # Record existing entries before adding two unique users
    status, initial_waitlist = get(path)
    assert status == 200, f"expected 200, got {status}: {initial_waitlist}"
    list_validator.validate(initial_waitlist)

    initial_ids = [entry["id"] for entry in initial_waitlist["entries"]]
    created_entries = []

    for offset in (1, 2):
        status, entry = post(
            path,
            json.dumps({"userId": f"test_{uuid4().hex}"}).encode("utf-8"),
            uuid4().hex,
        )
        assert status == 201, f"expected 201, got {status}: {entry}"
        post_validator.validate(entry)

        assert entry["eventId"] == event_id
        assert entry["position"] == len(initial_ids) + offset, \
            "new entry has the wrong position"
        created_entries.append(entry)

    first, second = created_entries

    # Listing must preserve arrival order and current positions
    status, waitlist = get(path)
    assert status == 200, f"expected 200, got {status}: {waitlist}"
    list_validator.validate(waitlist)

    assert [entry["id"] for entry in waitlist["entries"]] == \
        initial_ids + [first["id"], second["id"]], \
        "listing did not preserve first-come, first-served order"

    for position, entry in enumerate(waitlist["entries"], start=1):
        assert entry["position"] == position, \
            f"expected position {position}, got {entry['position']}"

    # Each entry must be retrievable by its stable ID
    for expected_entry in created_entries:
        status, entry = get(f"{path}/{expected_entry['id']}")
        assert status == 200, f"expected 200, got {status}: {entry}"
        entry_validator.validate(entry)
        assert entry == expected_entry, \
            "retrieved entry differs from the created entry"

    # Remove the earlier entry and verify it can no longer be retrieved
    status = delete(f"{path}/{first['id']}")
    assert status == 204, f"expected 204, got {status}"

    status, body = get(f"{path}/{first['id']}")
    assert status == 404, f"expected 404, got {status}: {body}"
    Draft202012Validator(
        schema_for(entry_contract_path, "get", "404")
    ).validate(body)

    assert body["error"] == "entry_not_found", \
        f"expected entry_not_found, got {body['error']}"

    # The remaining entry must move forward without changing its identity
    expected_second = {**second, "position": second["position"] - 1}
    status, entry = get(f"{path}/{second['id']}")
    assert status == 200, f"expected 200, got {status}: {entry}"
    entry_validator.validate(entry)
    assert entry == expected_second, \
        "remaining entry did not move forward with its other fields preserved"

    status, waitlist = get(path)
    assert status == 200, f"expected 200, got {status}: {waitlist}"
    list_validator.validate(waitlist)

    assert [entry["id"] for entry in waitlist["entries"]] == \
        initial_ids + [second["id"]], \
        "deletion removed the wrong entry or changed the order"

    for position, entry in enumerate(waitlist["entries"], start=1):
        assert entry["position"] == position, \
            f"expected position {position}, got {entry['position']}"

    assert waitlist["entries"][-1] == expected_second, \
        "listing and retrieval disagree about the remaining entry"

    # Remove the second entry created by this test
    status = delete(f"{path}/{second['id']}")
    assert status == 204, f"expected 204, got {status}"

# ---------------Runner -----------------------
if __name__ == "__main__":
    print("Conformance run against", BASE)
    check("GET /events matches the contract's response schema", t_list_shape)
    check("bookable=true filters and still matches the schema", t_bookable_filter)
    check("GET /events/{id} matches the Event schema", t_get_one)
    check("unknown id yields 404 with the Error schema", t_unknown_id)

    # Required waitlist checks
    print("Core Waitlist Tests:")
    check("Successful join of waitlist", t_successful_join)
    check("same-key retry replays without duplication", t_successful_replay)
    check("joining an event with available seats returns event_not_full", t_event_with_available_seats)
    check("unknown waitlist entry returns entry_not_found", t_unknown_entry)

    # Additional waitlist checks
    print("Additional Waitlist Tests:")
    check("same key with a different body returns idempotency_key_reuse",
          t_different_body_key_reuse)
    check("missing key returns idempotency_key_required",
          t_missing_idempotency_key)
    check("joining an unknown event returns event_not_found", t_unknown_event)
    check("same-key retry replays stored already_on_waitlist error",
          t_stored_already_on_waitlist_error)

    # Waitlist lifecycle checks
    print("Waitlist Lifecycle Tests:")
    check("listing, retrieval, deletion, and repositioning",
          t_waitlist_lifecycle)


    print(f"{CHECKS - FAILED}/{CHECKS} passed")
    sys.exit(1 if FAILED else 0)
