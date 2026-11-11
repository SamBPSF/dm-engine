# DM Engine

The referee for a solo D&D 5e game run by Claude. It owns the dice the DM rolls, hit points,
spell slots, combat order, hidden villain clocks and the sealed campaign notes, so the
narrating chat cannot fudge any of them.

## Files

- `engine.py`: the rules and saved game state (a SQLite file), plus the lore shelf: the player's own
  setting books, indexed for search in a second file so game backups stay small.
- `playbook.md`, `author.md`, `reviewer.md`, `setup.md`: the instructions the engine hands to the chat
  for running play, writing a campaign, reviewing a session and starting a new game.
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

    x-api-key: <your DM_KEY>

## Characters

Player characters are kept in a library, apart from any campaign. Each new campaign takes a rested copy.
What happens in play stays in that campaign; the library copy changes only when the player asks.

## Adding setting books

In a chat, ask the DM for a link to add books to a setting. It makes a private page, good for one hour,
where PDF, EPUB, Word and text files can be dropped in. The files are read into the search index and
not kept. `https://<domain>/backup` (with the key) downloads a copy of every saved game.

## Run locally

    pip install -r requirements.txt
    DM_KEY=anything python server.py
