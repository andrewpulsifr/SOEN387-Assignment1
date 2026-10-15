# Listing 3.1 -- Encore v1.1: reservations with an idempotency-key mechanism.
# The contract's new promise: a repeated POST with the same Idempotency-Key
# is recognized as a repeat and answered with the SAME outcome, applied once.
import hashlib
import json
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)
HERE = Path(__file__).resolve().parent

EVENTS = {
    "ev_101": {"id": "ev_101", "title": "Jazz Ensemble: Fall Concert", "seatsLeft": 42},
    "ev_102": {"id": "ev_102", "title": "Improv Night", "seatsLeft": 0},
    "ev_103": {"id": "ev_103", "title": "Film Society: Friday Screening", "seatsLeft": 2},
}
RESERVATIONS = {}          # id -> reservation
IDEMPOTENCY = {}           # key -> {"request_hash": ..., "status": ..., "body": ...}
NEXT = {"n": 1}

def body_hash():
    return hashlib.sha256(request.get_data() or b"").hexdigest()

@app.get("/events")
def list_events():
    events = list(EVENTS.values())
    if request.args.get("bookable") == "true":
        events = [e for e in events if e["seatsLeft"] > 0]
    return jsonify({"events": events})

@app.get("/events/<event_id>")
def get_event(event_id):
    ev = EVENTS.get(event_id)
    if ev is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(ev)

@app.post("/events/<event_id>/reservations")
def create_reservation(event_id):
    key = request.headers.get("Idempotency-Key")
    if not key:
        return jsonify({"error": "idempotency_key_required"}), 422

    seen = IDEMPOTENCY.get(key)
    if seen is not None:
        if seen["request_hash"] != body_hash():
            # Same key, different request: a client bug, and a dangerous one.
            return jsonify({"error": "idempotency_key_reuse"}), 422
        resp = jsonify(seen["body"])                    # replay the stored outcome
        resp.headers["Idempotency-Replayed"] = "true"
        return resp, seen["status"]

    # First time we see this key: perform the operation, then record the outcome.
    ev = EVENTS.get(event_id)
    if ev is None:
        outcome = ({"error": "not_found"}, 404)
    elif ev["seatsLeft"] <= 0:
        outcome = ({"error": "sold_out"}, 409)
    else:
        ev["seatsLeft"] -= 1
        rid = f"res_{NEXT['n']}"; NEXT["n"] += 1
        reservation = {"id": rid, "eventId": event_id,
                       "status": "confirmed", "createdAt": "2026-09-12T19:00:00Z"}
        RESERVATIONS[rid] = reservation
        outcome = (reservation, 201)

    IDEMPOTENCY[key] = {"request_hash": body_hash(),
                        "status": outcome[1], "body": outcome[0]}

    if request.headers.get("X-Demo-Lose-Reply") == "true":
        time.sleep(3)      # the work is DONE; only the reply is delayed past
                           # the client's timeout -- Chapter 1's third state.
    return jsonify(outcome[0]), outcome[1]

@app.get("/reservations/<res_id>")
def get_reservation(res_id):
    r = RESERVATIONS.get(res_id)
    if r is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(r)


@app.get("/openapi.yaml")
def openapi_yaml():
        return send_from_directory(HERE, "encore-openapi.yaml", mimetype="text/yaml")


@app.get("/apidocs")
@app.get("/apidocs/")
def apidocs():
        return """
<!doctype html>
<html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Encore API Docs</title>
        <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
    </head>
    <body>
        <div id="swagger-ui"></div>
        <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
        <script>
            window.ui = SwaggerUIBundle({
                url: '/openapi.yaml',
                dom_id: '#swagger-ui',
                deepLinking: true,
                presets: [SwaggerUIBundle.presets.apis],
                layout: 'BaseLayout'
            });
        </script>
    </body>
</html>
"""

if __name__ == "__main__":
    app.run(port=3000, threaded=True)
