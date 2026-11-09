# DM Engine

The referee for a solo D&D 5e game run by Claude. It owns the dice the DM rolls, hit points,
spell slots, combat order, hidden villain clocks and the sealed campaign notes, so the
narrating chat cannot fudge any of them.

## Files

- `engine.py`: the rules and saved game state (a SQLite file).
- `server.py`: exposes the engine to Claude as tools, locked behind a secret key.
- `test_engine.py`: rule checks. Run `python test_engine.py`.

## Deploy on Railway

1. Put these files in a private GitHub repository.
2. In Railway, create a new project from that repository.
3. Add a volume to the service and mount it at `/data`.
4. Set two variables on the service:
   - `DM_KEY`: a long random secret (40+ characters). Keep it private.
   - `DATA_DIR`: `/data`
5. Generate a public domain for the service. Opening `https://<domain>/health` should show `dm-engine ok`.

## Connect to Claude

Add a custom connector with the URL `https://<domain>/mcp` and a request header:

    Authorization: Bearer <your DM_KEY>

## Run locally

    pip install -r requirements.txt
    DM_KEY=anything python server.py
