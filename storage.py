EVENTS = {
    "ev_101": {"id": "ev_101", "title": "Jazz Ensemble: Fall Concert", "seatsLeft": 42},
    "ev_102": {"id": "ev_102", "title": "Improv Night", "seatsLeft": 0},
    "ev_103": {"id": "ev_103", "title": "Film Society: Friday Screening", "seatsLeft": 2},
}
RESERVATIONS = {}          # id -> reservation
IDEMPOTENCY = {}           # key -> {"request_target": ..., "request_hash": ...,
                          #          "status": ..., "body": ..., "stored_at": ...}
WAITLIST = {}              # event_id -> [waitlist entry, ...] in join order
NEXT = {"n": 1}
