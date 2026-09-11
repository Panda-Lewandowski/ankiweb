from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ankiweb.anki_core import AnkiAdapter
from ankiweb.app import create_app
from ankiweb.config import Settings


FIELDS = ["Prompt", "Answer", "Example", "Translation", "Language", "CEFR", "Topic", "Source"]


def _seed(col, *, spanish_cards: int = 5, english_cards: int = 1):
    model = col.models.new("Vocabulary Production")
    for field in FIELDS:
        col.models.add_field(model, col.models.new_field(field))
    template = col.models.new_template("Production")
    template["qfmt"] = "{{Prompt}}<br>{{type:Answer}}"
    template["afmt"] = "{{FrontSide}}<hr>{{Answer}}<br>{{Example}}"
    col.models.add_template(model, template)
    col.models.add_dict(model)

    ids = {"spanish": [], "english": []}
    for language, count in (("spanish", spanish_cards), ("english", english_cards)):
        deck = f"Languages::{language.title()}"
        did = col.decks.id(deck)
        for number in range(count):
            note = col.new_note(col.models.by_name("Vocabulary Production"))
            note["Prompt"] = f"prompt-{language}-{number}"
            note["Answer"] = f"answer-{language}-{number}"
            note["Example"] = f"example-{number}"
            note["Language"] = language
            note.tags = [f"language::{language}", "type::vocabulary"]
            col.add_note(note, did)
            ids[language].append(int(note.id))
    return ids


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "language.anki2")
    with TestClient(create_app(settings)) as test_client:
        test_client.portal.call(test_client.app.state.service.run, _seed)
        yield test_client


def _next(client, language="spanish", client_id="anonymous"):
    return client.get(
        f"/api/review/next?language={language}", headers={"X-Review-Client": client_id})


def test_health_and_exact_deck_today_counts(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["anki_core"] == {"anki_version": "25.9.4", "scheduler_version": 3}

    spanish = client.get("/api/today?language=spanish")
    english = client.get("/api/today?language=english")
    assert spanish.json()["deck"] == "Languages::Spanish"
    assert spanish.json()["new"] == 5
    assert english.json()["deck"] == "Languages::English"
    assert english.json()["new"] == 1


def test_missing_deck_is_not_created_and_empty_queue_is_204(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "empty.anki2")
    with TestClient(create_app(settings)) as empty:
        assert empty.get("/api/today?language=spanish").status_code == 409
        names = empty.portal.call(
            empty.app.state.service.run,
            lambda col: [item.name for item in col.decks.all_names_and_ids()],
        )
        assert "Languages::Spanish" not in names
        empty.portal.call(
            empty.app.state.service.run,
            lambda col: (col.decks.id("Languages::Spanish"), col.decks.id("Languages::English")),
        )
        assert _next(empty).status_code == 204


def test_topic_subdeck_is_rejected_instead_of_joining_language_queue(client):
    client.portal.call(
        client.app.state.service.run,
        lambda col: col.decks.id("Languages::Spanish::Grammar"),
    )
    response = client.get("/api/today?language=spanish")
    assert response.status_code == 409
    assert "subdecks" in response.json()["detail"]


def test_today_and_next_respect_anki_deck_limits(client):
    import anki.deck_config_pb2 as dc

    def set_limit(col):
        did = col.decks.id_for_name("Languages::Spanish")
        state = col.decks.get_deck_configs_for_update(did)
        deck_config = state.all_config[0].config
        deck_config.config.new_per_day = 2
        request = dc.UpdateDeckConfigsRequest(
            target_deck_id=did,
            configs=[deck_config],
            removed_config_ids=[],
            mode=dc.UpdateDeckConfigsMode.UPDATE_DECK_CONFIGS_MODE_NORMAL,
            card_state_customizer=state.card_state_customizer,
            limits=state.current_deck.limits,
            new_cards_ignore_review_limit=state.new_cards_ignore_review_limit,
            apply_all_parent_limits=state.apply_all_parent_limits,
            fsrs=state.fsrs,
            fsrs_reschedule=False,
        )
        return col.decks.update_deck_configs(request)

    client.portal.call(client.app.state.service.run_op, set_limit, "test-fixture")
    assert client.get("/api/today?language=spanish").json()["new"] == 2
    assert _next(client, client_id="limit-one").status_code == 200
    assert _next(client, client_id="limit-two").status_code == 200
    assert _next(client, client_id="limit-three").status_code == 204


def test_next_hides_answer_and_reuses_same_client_lease(client):
    first = _next(client)
    assert first.status_code == 200
    payload = first.json()
    assert payload["question"]["kind"] == "vocabulary_production"
    assert payload["question"]["input_required"] is True
    assert "Answer" not in str(payload)
    assert "answer-spanish" not in str(payload)
    assert _next(client).json()["token"] == payload["token"]


def test_concurrent_clients_receive_distinct_card_leases(client):
    first = _next(client, client_id="one").json()
    second = _next(client, client_id="two").json()
    assert first["token"] != second["token"]
    assert first["card_id"] != second["card_id"]


def test_review_token_is_scoped_to_client(client):
    issued = _next(client, client_id="owner").json()
    response = client.post(
        f"/api/review/{issued['token']}/check",
        headers={"X-Review-Client": "other"},
        json={"typed_answer": "x"},
    )
    assert response.status_code == 409
    assert "different client" in response.json()["detail"]


def test_tts_rejects_non_listening_card(client):
    issued = _next(client).json()
    response = client.get(f"/api/review/{issued['token']}/audio")
    assert response.status_code == 409
    assert "only available for listening" in response.json()["detail"]


def test_check_reveals_without_scheduling_and_answer_is_exactly_once(client):
    issued = _next(client).json()
    before = client.portal.call(
        client.app.state.service.run,
        lambda col: (len(col.get_review_logs(issued["card_id"])), col.get_card(issued["card_id"]).reps),
    )
    answer_number = issued["question"]["prompt_html"].rsplit("-", 1)[1]
    checked = client.post(
        f"/api/review/{issued['token']}/check",
        json={"typed_answer": f"  answer-spanish-{answer_number}  "},
    )
    assert checked.status_code == 200
    assert checked.json()["correct"] is True
    assert checked.json()["back"]["fields"]["Answer"] == f"answer-spanish-{answer_number}"
    after_check = client.portal.call(
        client.app.state.service.run,
        lambda col: (len(col.get_review_logs(issued["card_id"])), col.get_card(issued["card_id"]).reps),
    )
    assert after_check == before

    answered = client.post(f"/api/review/{issued['token']}/answer", json={"rating": "good"})
    assert answered.status_code == 200
    after_answer = client.portal.call(
        client.app.state.service.run,
        lambda col: (len(col.get_review_logs(issued["card_id"])), col.get_card(issued["card_id"]).reps),
    )
    assert after_answer == (before[0] + 1, before[1] + 1)
    assert client.post(
        f"/api/review/{issued['token']}/answer", json={"rating": "good"}).status_code == 409


def test_last_session_answer_does_not_issue_another_card(client):
    issued = _next(client).json()
    client.post(f"/api/review/{issued['token']}/check", json={"typed_answer": "wrong"})
    response = client.post(
        f"/api/review/{issued['token']}/answer",
        json={"rating": "again", "continue_session": False},
    )
    assert response.status_code == 200
    assert response.json()["answered"] is True
    assert response.json()["next"] is None

    # Ending the UI session does not alter Anki's queue; a later session can ask Core again.
    later = _next(client).json()
    assert later["card_id"] != issued["card_id"] or later["token"] != issued["token"]


def test_rating_requires_reveal(client):
    issued = _next(client).json()
    response = client.post(f"/api/review/{issued['token']}/answer", json={"rating": "easy"})
    assert response.status_code == 409
    assert "reveal" in response.json()["detail"]


@pytest.mark.parametrize("rating", ["again", "hard", "good", "easy"])
def test_all_ratings_delegate_to_real_scheduler(tmp_path: Path, rating: str):
    settings = Settings(collection_path=tmp_path / f"{rating}.anki2")
    with TestClient(create_app(settings)) as client:
        client.portal.call(client.app.state.service.run, lambda col: _seed(col, spanish_cards=1, english_cards=0))
        issued = _next(client).json()
        client.post(f"/api/review/{issued['token']}/check", json={"typed_answer": "wrong"})
        response = client.post(f"/api/review/{issued['token']}/answer", json={"rating": rating})
        assert response.status_code == 200
        logs = client.portal.call(
            client.app.state.service.run, lambda col: col.get_review_logs(issued["card_id"]))
        assert len(logs) == 1


def test_stale_card_state_is_rejected(client):
    issued = _next(client).json()
    client.post(f"/api/review/{issued['token']}/check", json={"typed_answer": "wrong"})
    client.portal.call(
        client.app.state.service.run_op,
        lambda col: col.sched.suspend_cards([issued["card_id"]]),
        "test",
    )
    response = client.post(f"/api/review/{issued['token']}/answer", json={"rating": "again"})
    assert response.status_code == 409
    assert "changed" in response.json()["detail"]


def test_expired_and_unknown_tokens_are_distinct(client):
    client.app.state.anki_adapter = AnkiAdapter(client.app.state.service, token_ttl_seconds=0)
    issued = _next(client).json()
    assert client.post(
        f"/api/review/{issued['token']}/check", json={"typed_answer": "x"}).status_code == 410
    assert client.post(
        "/api/review/not-a-token/check", json={"typed_answer": "x"}).status_code == 404


def test_single_card_suspend_unsuspend_and_bury(client):
    issued = _next(client).json()
    card_id = issued["card_id"]
    assert client.post(f"/api/cards/{card_id}/suspend").status_code == 200
    queue = client.portal.call(client.app.state.service.run, lambda col: int(col.get_card(card_id).queue))
    assert queue == -1
    assert client.post(f"/api/cards/{card_id}/unsuspend").status_code == 200
    assert client.post(f"/api/cards/{card_id}/bury").status_code == 200
    queue = client.portal.call(client.app.state.service.run, lambda col: int(col.get_card(card_id).queue))
    assert queue < 0


def test_card_action_rejects_non_language_deck(client):
    def add_default(col):
        note = col.new_note(col.models.by_name("Basic"))
        note["Front"] = "outside"
        note["Back"] = "outside-answer"
        col.add_note(note, col.decks.id("Default"))
        return int(col.find_cards("nid:" + str(note.id))[0])

    card_id = client.portal.call(client.app.state.service.run, add_default)
    assert client.post(f"/api/cards/{card_id}/suspend").status_code == 409
    assert client.post("/api/cards/999999999999/suspend").status_code == 404


def test_answer_state_and_history_survive_collection_reopen(tmp_path: Path):
    path = tmp_path / "reopen.anki2"
    settings = Settings(collection_path=path)
    with TestClient(create_app(settings)) as first:
        first.portal.call(first.app.state.service.run, lambda col: _seed(col, spanish_cards=1, english_cards=0))
        issued = _next(first).json()
        first.post(f"/api/review/{issued['token']}/check", json={"typed_answer": "answer-spanish-0"})
        assert first.post(
            f"/api/review/{issued['token']}/answer", json={"rating": "easy"}).status_code == 200
        card_id = issued["card_id"]
        before = first.portal.call(first.app.state.service.run, lambda col: (
            col.get_card(card_id).queue, col.get_card(card_id).type,
            col.get_card(card_id).due, col.get_card(card_id).ivl,
            col.get_card(card_id).memory_state.SerializeToString()
            if col.get_card(card_id).memory_state is not None else b"",
            len(col.get_review_logs(card_id)),
        ))

    with TestClient(create_app(settings)) as reopened:
        after = reopened.portal.call(reopened.app.state.service.run, lambda col: (
            col.get_card(card_id).queue, col.get_card(card_id).type,
            col.get_card(card_id).due, col.get_card(card_id).ivl,
            col.get_card(card_id).memory_state.SerializeToString()
            if col.get_card(card_id).memory_state is not None else b"",
            len(col.get_review_logs(card_id)),
        ))
    assert after == before


def test_real_scheduler_new_learning_review_relearning_and_repeated_lapse(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "states.anki2")
    with TestClient(create_app(settings)) as client:
        client.portal.call(client.app.state.service.run, lambda col: _seed(col, spanish_cards=1, english_cards=0))

        def state(card_id):
            return client.portal.call(client.app.state.service.run, lambda col: (
                int(col.get_card(card_id).queue), int(col.get_card(card_id).type),
                col.get_card(card_id).lapses,
            ))

        def rate(issued, rating):
            client.post(f"/api/review/{issued['token']}/check", json={"typed_answer": "x"})
            response = client.post(
                f"/api/review/{issued['token']}/answer", json={"rating": rating})
            assert response.status_code == 200
            return response.json()["next"]

        issued = _next(client).json()
        card_id = issued["card_id"]
        assert state(card_id)[:2] == (0, 0)  # New
        learning = rate(issued, "good")
        assert learning is not None
        assert state(card_id)[:2] == (1, 1)  # Learning
        assert rate(learning, "easy") is None
        assert state(card_id)[:2] == (2, 2)  # Review

        client.portal.call(
            client.app.state.service.run_op,
            lambda col: col.sched.set_due_date([card_id], "0"),
            "test-fixture",
        )
        review = _next(client).json()
        relearning = rate(review, "again")
        assert relearning is not None
        assert state(card_id) == (1, 3, 1)  # Relearning after first lapse
        rate(relearning, "easy")

        client.portal.call(
            client.app.state.service.run_op,
            lambda col: col.sched.set_due_date([card_id], "0"),
            "test-fixture",
        )
        second_review = _next(client).json()
        rate(second_review, "again")
        assert state(card_id)[1:] == (3, 2)


def test_cloze_sibling_and_expected_answer_boundary(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "cloze.anki2")
    with TestClient(create_app(settings)) as client:
        def seed_cloze(col):
            did = col.decks.id("Languages::Spanish")
            col.decks.id("Languages::English")
            note = col.new_note(col.models.by_name("Cloze"))
            note["Text"] = "No creo que ella {{c1::tenga}} razón {{c2::ahora}}."
            note.tags = ["language::spanish", "type::cloze"]
            col.add_note(note, did)
            return [int(card_id) for card_id in col.find_cards(f"nid:{note.id}")]

        sibling_ids = client.portal.call(client.app.state.service.run, seed_cloze)
        assert len(sibling_ids) == 2
        issued = _next(client).json()
        assert issued["question"]["kind"] == "grammar_cloze"
        assert "tenga" not in issued["question"]["prompt_html"]
        checked = client.post(
            f"/api/review/{issued['token']}/check", json={"typed_answer": "tenga"})
        assert checked.status_code == 200
        assert checked.json()["correct"] is True
        assert checked.json()["back"]["fields"]["Answer"] == "tenga"
        assert "{{c" not in checked.json()["back"]["fields"]["Text"]
        answered = client.post(
            f"/api/review/{issued['token']}/answer", json={"rating": "easy"}).json()
        other_id = next(cid for cid in sibling_ids if cid != issued["card_id"])
        # Whether the sibling is buried is deck-config/scheduler policy. If Anki queues it,
        # the adapter must preserve its separate card identity and issue it normally.
        if answered["next"] is not None:
            assert answered["next"]["card_id"] == other_id


def test_listening_dictation_does_not_expose_sentence_before_check(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "listening.anki2")
    spoken = []

    def fake_tts(text, locale):
        spoken.append((text, locale))
        return b"RIFF-safe-audio"

    with TestClient(create_app(settings, tts_synthesizer=fake_tts)) as client:
        def seed_listening(col):
            model = col.models.new("Listening Dictation")
            for field in ("Sentence", "Translation", "Note", "Language", "CEFR", "Topic", "Source"):
                col.models.add_field(model, col.models.new_field(field))
            template = col.models.new_template("Dictation")
            template["qfmt"] = "{{tts es_ES:Sentence}}"
            template["afmt"] = "{{Sentence}}<hr>{{Translation}}"
            col.models.add_template(model, template)
            col.models.add_dict(model)
            did = col.decks.id("Languages::Spanish")
            col.decks.id("Languages::English")
            note = col.new_note(col.models.by_name("Listening Dictation"))
            note["Sentence"] = "No creo que tenga razón."
            note["Translation"] = "Не думаю, что она права."
            note.tags = ["language::spanish", "type::listening_dictation"]
            col.add_note(note, did)

        client.portal.call(client.app.state.service.run, seed_listening)
        issued = _next(client).json()
        question = issued["question"]
        assert question["prompt_html"] == ""
        assert question["tts_locale"] == "es_ES"
        assert "No creo" not in str(issued)
        audio = client.get(f"/api/review/{issued['token']}/audio")
        assert audio.status_code == 200
        assert audio.headers["content-type"] == "audio/wav"
        assert audio.headers["cache-control"] == "private, no-store"
        assert audio.content == b"RIFF-safe-audio"
        assert spoken == [("No creo que tenga razón.", "es_ES")]
        assert client.get(
            f"/api/review/{issued['token']}/audio",
            headers={"X-Review-Client": "another-client"},
        ).status_code == 409
        checked = client.post(
            f"/api/review/{issued['token']}/check",
            json={"typed_answer": "No creo que tenga razón."},
        )
        assert checked.json()["correct"] is True
        assert checked.json()["back"]["fields"]["Sentence"] == "No creo que tenga razón."


def test_phrase_retrieval_uses_typed_answer_without_exposing_answer(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "phrase.anki2")
    with TestClient(create_app(settings)) as client:
        def seed_phrase(col):
            model = col.models.new("Phrase Retrieval")
            for field in ("Prompt", "Answer", "Example", "Language", "CEFR", "Topic", "Source"):
                col.models.add_field(model, col.models.new_field(field))
            template = col.models.new_template("Phrase")
            template["qfmt"] = "{{Prompt}}"
            template["afmt"] = "{{Answer}}<hr>{{Example}}"
            col.models.add_template(model, template)
            col.models.add_dict(model)
            did = col.decks.id("Languages::English")
            col.decks.id("Languages::Spanish")
            note = col.new_note(col.models.by_name("Phrase Retrieval"))
            note["Prompt"] = "Дай мне секунду подумать."
            note["Answer"] = "Let me think about that for a second."
            note["Example"] = "Let me think before I answer."
            note["CEFR"] = "B2"
            note["Topic"] = "fluency_chunks"
            note.tags = ["language::english", "type::chunk"]
            col.add_note(note, did)

        client.portal.call(client.app.state.service.run, seed_phrase)
        issued = _next(client, language="english").json()
        assert issued["question"]["kind"] == "phrase_retrieval"
        assert issued["question"]["input_required"] is True
        assert issued["question"]["topic"] == "fluency_chunks"
        assert issued["question"]["cefr"] == "B2"
        assert "Let me think" not in str(issued)
        checked = client.post(
            f"/api/review/{issued['token']}/check",
            json={"typed_answer": "Let me think about that for a second."},
        )
        assert checked.json()["correct"] is True


def test_personal_error_never_returns_original_error(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "personal-error.anki2")
    with TestClient(create_app(settings)) as client:
        def seed_personal_error(col):
            model = col.models.new("Personal Error")
            for field in (
                "Prompt", "Answer", "Explanation", "OriginalError",
                "Language", "CEFR", "Topic", "Source",
            ):
                col.models.add_field(model, col.models.new_field(field))
            template = col.models.new_template("Error")
            template["qfmt"] = "{{Prompt}}"
            template["afmt"] = "{{Answer}}<hr>{{Explanation}}"
            col.models.add_template(model, template)
            col.models.add_dict(model)
            did = col.decks.id("Languages::English")
            col.decks.id("Languages::Spanish")
            note = col.new_note(col.models.by_name("Personal Error"))
            note["Prompt"] = "Our priorities ___ recently."
            note["Answer"] = "have changed"
            note["Explanation"] = "Present perfect with a plural subject."
            note["OriginalError"] = "Our priorities has changing."
            note.tags = ["language::english", "type::personal_error"]
            col.add_note(note, did)

        client.portal.call(client.app.state.service.run, seed_personal_error)
        issued = _next(client, language="english").json()
        assert "has changing" not in str(issued)
        checked = client.post(
            f"/api/review/{issued['token']}/check", json={"typed_answer": "have changed"})
        assert checked.status_code == 200
        assert "OriginalError" not in checked.json()["back"]["fields"]
        assert "has changing" not in str(checked.json())


def test_product_boundary_contains_no_manual_scheduler_or_sql_path():
    root = Path(__file__).parents[2] / "ankiweb"
    source = "\n".join(
        path.read_text() for directory in (root / "anki_core", root / "api")
        for path in directory.glob("*.py")
    )
    for forbidden in (
        "._backend", ".db.", "nextIvl", "describe_next_states",
        "set_due_date", "set_scheduling_states", "compute_fsrs", "simulate_fsrs",
        "date.today", "datetime.now",
    ):
        assert forbidden not in source
    assert "build_answer(" in source
    assert "answer_card(" in source
