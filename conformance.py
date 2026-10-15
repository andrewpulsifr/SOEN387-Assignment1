# Listing 2.3 -- A conformance suite written against the CONTRACT, not the code.
# It reads encore-openapi.yaml, extracts the response schemas, and checks a
# running implementation against them. It never imports the server's source.
# Run (with a server on :3000): python3 conformance.py
import json, sys, urllib.request, urllib.error
import yaml
from jsonschema import Draft202012Validator

BASE = "http://localhost:3000"
spec = yaml.safe_load(open("encore-openapi.yaml"))

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

if __name__ == "__main__":
    print("Conformance run against", BASE)
    check("GET /events matches the contract's response schema", t_list_shape)
    check("bookable=true filters and still matches the schema", t_bookable_filter)
    check("GET /events/{id} matches the Event schema", t_get_one)
    check("unknown id yields 404 with the Error schema", t_unknown_id)
    print(f"{CHECKS - FAILED}/{CHECKS} passed")
    sys.exit(1 if FAILED else 0)
