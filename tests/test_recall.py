"""Searching past conversations, facts and lessons (recall.py)."""
from argus import recall
from argus.store import Store

NOW = 1_800_000_000.0


def seeded(tmp_path):
    store = Store(tmp_path / "a.db")
    old = store.new_session("voice", "mac")
    store.append(old, "user", "I still need a hotel in Midtown for the New York trip")
    store.append(old, "assistant", [{"type": "text", "text": "I'll keep an eye on Midtown hotel prices."},
                                    {"type": "tool_use", "id": "t", "name": "follow_up", "input": {}}])
    store.append(old, "user", [{"type": "tool_result", "tool_use_id": "t", "content": "Watching it (#3)."}])
    store.append(old, "assistant", "Udupi Palace is the pick for dosas, 4.4 stars.")
    store.append(old, "user", "(I interrupted you)")
    guest = store.new_session("guest", "mac")
    store.append(guest, "user", "my secret hotel plans")
    store.remember("Sister's birthday is 12 March")
    store.add_lesson("Ask before booking anything over 100 dollars")
    store._q("UPDATE messages SET created=?", (NOW - 3600,))
    return store


def test_finds_what_was_said_with_stemming_and_labels(tmp_path):
    store = seeded(tmp_path)
    out = recall.search(store, "hotels booked midtown", now=NOW)
    assert "[you said, " in out and "Midtown for the New York trip" in out and "[Argus said, " in out
    assert "secret" not in out                               # guests' conversations aren't searched
    assert "Udupi Palace" in recall.search(store, "dosa place", now=NOW)
    assert "[saved fact] Sister's birthday" in recall.search(store, "birthday", now=NOW)
    assert "[lesson]" in recall.search(store, "booking", now=NOW)
    assert recall.search(store, "the and of", now=NOW).startswith("Nothing found")
    assert recall.search(store, "zebra", now=NOW, days=7).startswith("Nothing found about that in the last 7 days")
    assert recall.search(store, "midtown", now=NOW + 40 * 86400).count("[you said") == 0   # older than 30 days


def test_index_and_helpers():
    assert recall.stem("booking") == "book" and recall.stem("cities") == "city" and recall.stem("is") == "is"
    assert recall.text_of(42) == "" and recall.text_of([1, {"type": "image"}]) == ""
    assert recall.text_of([{"type": "tool_result", "content": None}]) == ""
    index = recall.Index([("hotel hotel hotel", {"at": None}), ("one hotel among many other words here", {"at": None})])
    first = index.search("hotel")
    assert first[0][1] == "hotel hotel hotel"
    assert recall.Index([]).search("x") == []


def test_the_recall_tool(tmp_path):
    store = seeded(tmp_path)
    tools = {}
    recall.attach(store, tools, clock=lambda: NOW)
    tool = tools["recall"]
    assert "Midtown" in tool.run({"query": "hotel"}, None).content
    assert "Midtown" in tool.run({"query": "hotel", "days": 500}, None).content
    assert tool.run({"query": "  "}, None).is_error
    assert tool.channels == frozenset({"voice", "phone", "cli", "android"})


def test_bad_rows_are_skipped(tmp_path):
    store = seeded(tmp_path)
    store._q("UPDATE messages SET content='not json' WHERE id=1")
    assert len(store.all_messages(since=0)) == 4
