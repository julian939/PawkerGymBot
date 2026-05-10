CREATE TABLE IF NOT EXISTS challenges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    challenger_id   INTEGER NOT NULL,
    opponent_id     INTEGER,                       -- NULL = open challenge
    challenge_type  TEXT NOT NULL CHECK (challenge_type IN ('attack', 'defend')),
    status          TEXT NOT NULL CHECK (status IN ('PENDING', 'ACCEPTED', 'CANCELLED', 'EXPIRED')),
    room_code       TEXT,                          -- set only when ACCEPTED
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    accepted_at     TEXT,
    cancelled_at    TEXT,
    cancelled_by    INTEGER,
    expires_at      TEXT NOT NULL,
    admin_message_id INTEGER                       -- Discord message id of the admin-channel log
);

CREATE UNIQUE INDEX IF NOT EXISTS challenges_room_code_uniq
    ON challenges (room_code) WHERE room_code IS NOT NULL;

CREATE INDEX IF NOT EXISTS challenges_queue_lookup
    ON challenges (guild_id, status, challenge_type, opponent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS challenges_challenger_status
    ON challenges (challenger_id, status);

CREATE INDEX IF NOT EXISTS challenges_opponent_status
    ON challenges (opponent_id, status);

CREATE INDEX IF NOT EXISTS challenges_expiry
    ON challenges (status, expires_at);

CREATE INDEX IF NOT EXISTS challenges_message_id
    ON challenges (message_id);
