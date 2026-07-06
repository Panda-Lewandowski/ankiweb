#!/usr/bin/env python
"""Drive the legacy stats renderer directly against PG (throwaway schema) —
fast iteration on remaining dialect issues, no server restarts. Also renders
against a throwaway SQLite collection with the same data as the parity check."""
import sys, time, traceback

import os
# base DSN from env (same var the server uses); append a throwaway schema search_path
_BASE_DSN = os.environ["ANKIWEB_PG_DSN"]
DSN = _BASE_DSN + ("&" if "?" in _BASE_DSN else "?") + "options=-csearch_path%3Dstatsprof,public"
import psycopg
with psycopg.connect(DSN.split("?")[0], autocommit=True) as c:
    c.execute("DROP SCHEMA IF EXISTS statsprof CASCADE")
    c.execute("CREATE SCHEMA statsprof")

import anki.collection


def populate(col):
    did = col.decks.id("StatsDeck")
    m = col.models.by_name("Basic")
    for i in range(8):
        n = col.new_note(m)
        n["Front"], n["Back"] = f"sf-{i}", "b"
        col.add_note(n, did)
    # answer a few cards so revlog has lrn entries
    from anki.collection import Collection  # noqa
    cids = col.find_cards("deck:StatsDeck")
    for cid in cids[:4]:
        card = col.get_card(cid)
        card.start_timer()
        states = col._backend.get_scheduling_states(cid)
        answer = col.sched.build_answer(card=card, states=states, rating=3)
        col.sched.answer_card(answer)


def render(col, whole):
    stats = col.stats()
    try:
        stats.wholeCollection = whole
    except Exception:
        pass
    return stats.report()


import os
os.makedirs("/tmp/claude-0/statsprof-media", exist_ok=True)
pgcol = anki.collection.Collection("/tmp/claude-0/statsprof-media/collection.anki2",
                                   server=False, pg_dsn=DSN)
populate(pgcol)
ok = True
for whole in (True, False):
    try:
        html = render(pgcol, whole)
        print(f"PG whole={whole}: OK, {len(html)} bytes")
    except Exception:
        ok = False
        tb = traceback.format_exc()
        tail = [l for l in tb.splitlines() if 'DbError' in l or 'Error' in l][-1:]
        print(f"PG whole={whole}: FAIL\n  " + (tail[0][:500] if tail else tb[-500:]))
pgcol.close()
with psycopg.connect(DSN.split("?")[0], autocommit=True) as c:
    c.execute("DROP SCHEMA IF EXISTS statsprof CASCADE")
print("PG_STATS_OK" if ok else "PG_STATS_FAIL")
sys.exit(0 if ok else 1)
