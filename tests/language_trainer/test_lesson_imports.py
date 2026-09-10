from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from anki.consts import MODEL_CLOZE

from ankiweb.app import create_app
from ankiweb.config import Settings
from ankiweb.language.card_types import CARD_TYPES


def _install_models(col, keys=("vocabulary_production",)):
    col.decks.id("Languages::Spanish")
    col.decks.id("Languages::English")
    for key in keys:
        spec = CARD_TYPES[key]
        if col.models.by_name(spec.model_name):
            continue
        model = col.models.new(spec.model_name)
        model["type"] = MODEL_CLOZE if spec.cloze else 0
        for field in spec.fields:
            col.models.add_field(model, col.models.new_field(field))
        template = col.models.new_template("Card")
        template["qfmt"] = "{{cloze:Text}}" if spec.cloze else "{{" + spec.fields[0] + "}}"
        template["afmt"] = "{{cloze:Text}}" if spec.cloze else "{{FrontSide}}"
        col.models.add_template(model, template)
        col.models.add_dict(model)


def _payload(*cards, commit=False, language="spanish"):
    return {
        "lesson": {
            "language": language,
            "source": "chatgpt_lesson",
            "date": "2026-09-10",
        },
        "cards": list(cards),
        "commit": commit,
    }


def _vocabulary(**overrides):
    card = {
        "type": "vocabulary_production",
        "prompt": "Как сказать: воспользоваться возможностью?",
        "answer": "aprovechar la oportunidad",
        "example": "Hay que aprovechar esta oportunidad.",
        "cefr": "B1",
        "topic": "opportunities",
    }
    card.update(overrides)
    return card


def _client(settings: Settings):
    return TestClient(create_app(settings))


def test_preview_is_read_only_and_reports_canonical_target(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "preview.anki2")
    with _client(settings) as client:
        client.portal.call(client.app.state.service.run, _install_models)
        response = client.post("/api/lesson-cards/batch", json=_payload(_vocabulary()))
        assert response.status_code == 200
        result = response.json()
        assert result["mode"] == "preview"
        assert result["summary"] == {
            "added": 0, "would_add": 1, "updated_metadata": 0,
            "would_update_metadata": 0, "duplicates": 0, "error_count": 0,
        }
        assert result["items"][0]["deck"] == "Languages::Spanish"
        assert result["items"][0]["model"] == "Vocabulary Production"
        assert result["receipt_id"] is None
        note_count = client.portal.call(
            client.app.state.service.run,
            lambda col: len(col.find_notes('deck:"Languages::Spanish"')),
        )
        assert note_count == 0
        assert not (tmp_path / "lesson-receipts").exists()


def test_commit_adds_to_exact_deck_with_canonical_tags_and_durable_receipt(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "commit.anki2")
    with _client(settings) as client:
        client.portal.call(client.app.state.service.run, _install_models)
        result = client.post(
            "/api/lesson-cards/batch", json=_payload(_vocabulary(), commit=True),
        ).json()
        assert result["added"] == 1
        assert result["errors"] == []
        assert result["receipt_id"]
        note_id = result["items"][0]["note_id"]

        note_data = client.portal.call(
            client.app.state.service.run,
            lambda col: (
                col.decks.name(col.get_card(col.find_cards(f"nid:{note_id}")[0]).did),
                col.get_note(note_id).tags,
                col.get_note(note_id)["Language"],
                col.get_note(note_id)["Source"],
            ),
        )
        assert note_data[0] == "Languages::Spanish"
        assert set(note_data[1]) == {
            "language::spanish", "cefr::B1", "topic::opportunities",
            "source::chatgpt_lesson", "type::vocabulary",
        }
        assert note_data[2:] == ("spanish", "chatgpt_lesson")

    with _client(settings) as reopened:
        receipts = reopened.get("/api/lesson-cards/receipts").json()
        assert receipts[0]["receipt_id"] == result["receipt_id"]
        assert receipts[0]["summary"]["added"] == 1
        # Durable audit metadata intentionally excludes complete card text.
        assert "aprovechar la oportunidad" not in str(receipts)


def test_normalized_duplicate_only_merges_blank_metadata_and_preserves_review_state(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "duplicate.anki2")
    with _client(settings) as client:
        def seed(col):
            _install_models(col)
            note = col.new_note(col.models.by_name("Vocabulary Production"))
            note["Prompt"] = "<b>Как сказать: воспользоваться возможностью!</b>"
            note["Answer"] = "APROVECHAR LA OPORTUNIDAD."
            note["Language"] = "spanish"
            note.tags = ["language::spanish", "type::vocabulary"]
            col.add_note(note, col.decks.id_for_name("Languages::Spanish"))
            return int(note.id)

        note_id = client.portal.call(client.app.state.service.run, seed)
        issued = client.get("/api/review/next?language=spanish").json()
        client.post(
            f"/api/review/{issued['token']}/check",
            json={"typed_answer": "APROVECHAR LA OPORTUNIDAD."},
        )
        client.post(f"/api/review/{issued['token']}/answer", json={"rating": "good"})

        def state(col):
            card = col.get_card(col.find_cards(f"nid:{note_id}")[0])
            memory = card.memory_state.SerializeToString() if card.memory_state is not None else b""
            return (
                int(card.id), int(card.queue), int(card.type), card.due, card.ivl, card.reps,
                card.lapses, memory, len(col.get_review_logs(card.id)),
            )

        before = client.portal.call(client.app.state.service.run, state)
        preview = client.post("/api/lesson-cards/batch", json=_payload(_vocabulary())).json()
        assert preview["items"][0]["status"] == "would_update_metadata"
        assert preview["items"][0]["filled_fields"] == ["CEFR", "Source", "Topic"]

        committed = client.post(
            "/api/lesson-cards/batch", json=_payload(_vocabulary(), commit=True),
        ).json()
        assert committed["updated_metadata"] == 1
        assert committed["added"] == 0
        after = client.portal.call(client.app.state.service.run, state)
        assert after == before
        metadata = client.portal.call(
            client.app.state.service.run,
            lambda col: (
                col.get_note(note_id)["CEFR"], col.get_note(note_id)["Topic"],
                col.get_note(note_id)["Source"], col.get_note(note_id).tags,
            ),
        )
        assert metadata[:3] == ("B1", "opportunities", "chatgpt_lesson")
        assert "source::chatgpt_lesson" in metadata[3]


def test_different_production_prompt_with_same_answer_remains_a_distinct_card(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "production.anki2")
    with _client(settings) as client:
        client.portal.call(client.app.state.service.run, _install_models)
        cards = (
            _vocabulary(),
            _vocabulary(prompt="Как сказать: использовать шанс?", example="Debemos aprovecharla."),
        )
        result = client.post(
            "/api/lesson-cards/batch", json=_payload(*cards, commit=True),
        ).json()
        assert result["added"] == 2
        assert result["duplicates"] == 0


def test_conflicting_cefr_is_reported_without_overwrite_or_conflicting_tag(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "metadata-conflict.anki2")
    with _client(settings) as client:
        def seed(col):
            _install_models(col)
            note = col.new_note(col.models.by_name("Vocabulary Production"))
            note["Prompt"] = _vocabulary()["prompt"]
            note["Answer"] = _vocabulary()["answer"]
            note["Language"] = "spanish"
            note["CEFR"] = "B2"
            note.tags = ["language::spanish", "cefr::B2", "type::vocabulary"]
            col.add_note(note, col.decks.id_for_name("Languages::Spanish"))
            return int(note.id)

        note_id = client.portal.call(client.app.state.service.run, seed)
        result = client.post(
            "/api/lesson-cards/batch", json=_payload(_vocabulary(), commit=True),
        ).json()
        assert result["added"] == 0
        assert result["updated_metadata"] == 0
        assert result["items"][0]["status"] == "metadata_conflict"
        assert result["items"][0]["conflicting_fields"] == ["CEFR"]
        metadata = client.portal.call(
            client.app.state.service.run,
            lambda col: (col.get_note(note_id)["CEFR"], col.get_note(note_id).tags),
        )
        assert metadata[0] == "B2"
        assert "cefr::B1" not in metadata[1]


def test_duplicate_inside_batch_is_skipped_and_accents_remain_significant(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "batch-duplicate.anki2")
    with _client(settings) as client:
        client.portal.call(client.app.state.service.run, _install_models)
        exact = _vocabulary()
        duplicate = _vocabulary(
            prompt="  КАК СКАЗАТЬ воспользоваться возможностью ",
            answer="Aprovechar la oportunidad!",
        )
        accented = _vocabulary(
            prompt="Выразить согласие", answer="sí", topic="agreement",
        )
        unaccented = _vocabulary(
            prompt="Условие", answer="si", topic="conditions",
        )
        preview = client.post(
            "/api/lesson-cards/batch", json=_payload(exact, duplicate, accented, unaccented),
        ).json()
        assert [item["status"] for item in preview["items"]] == [
            "would_add", "skipped_duplicate", "would_add", "would_add",
        ]
        assert preview["duplicates"] == 1


def test_invalid_type_model_and_english_conjugation_are_reported_without_mutation(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "invalid.anki2")
    with _client(settings) as client:
        client.portal.call(
            client.app.state.service.run,
            lambda col: (col.decks.id("Languages::Spanish"), col.decks.id("Languages::English")),
        )
        cards = [
            _vocabulary(type="unknown"),
            _vocabulary(),
        ]
        result = client.post("/api/lesson-cards/batch", json=_payload(*cards)).json()
        assert result["error_count"] == 2
        assert "unsupported card type" in result["errors"][0]["message"]
        assert "note type does not exist" in result["errors"][1]["message"]

        conjugation = {
            "type": "conjugation", "verb": "hacer", "tense": "Indefinido",
            "person": "él", "answer": "hizo", "cefr": "B1", "topic": "past",
        }
        english = client.post(
            "/api/lesson-cards/batch",
            json=_payload(conjugation, language="english"),
        ).json()
        assert english["error_count"] == 1
        assert "Spanish lesson" in english["errors"][0]["message"]


def test_request_shape_and_batch_limit_are_validated(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "validation.anki2")
    with _client(settings) as client:
        assert client.post("/api/lesson-cards/batch", json={}).status_code == 422
        payload = _payload(*[_vocabulary(prompt=f"prompt {i}") for i in range(51)])
        assert client.post("/api/lesson-cards/batch", json=payload).status_code == 422


def test_all_eight_stable_card_schemas_import_through_the_adapter(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "all-types.anki2")
    with _client(settings) as client:
        client.portal.call(client.app.state.service.run, lambda col: _install_models(col, CARD_TYPES))
        cards = [
            _vocabulary(),
            {"type": "vocabulary_recognition", "target": "aprovechar", "translation": "использовать",
             "cefr": "B1", "topic": "vocabulary"},
            {"type": "grammar_cloze", "text": "No creo que {{c1::tenga}} razón.",
             "back_extra": "No creo que + subjuntivo", "cefr": "B1", "topic": "subjuntivo"},
            {"type": "personal_error", "prompt": "Ella ___ razón. (tener)", "answer": "tiene",
             "explanation": "Presente", "original_error": "Ella tener razón.",
             "cefr": "B1", "topic": "errors"},
            {"type": "conjugation", "verb": "hacer", "tense": "Pretérito indefinido",
             "person": "él/ella", "answer": "hizo", "example": "Ella hizo una reserva.",
             "cefr": "B1", "topic": "past"},
            {"type": "listening_dictation", "sentence": "No creo que tenga razón.",
             "translation": "Не думаю, что она права.", "cefr": "B1", "topic": "listening"},
            {"type": "listening_comprehension", "sentence": "A ver qué podemos hacer.",
             "translation": "Посмотрим, что можно сделать.", "cefr": "B1", "topic": "listening"},
            {"type": "phrase_retrieval", "prompt": "Дай мне секунду подумать.",
             "answer": "Déjame pensarlo un segundo.", "cefr": "B1", "topic": "fluency"},
        ]
        result = client.post(
            "/api/lesson-cards/batch", json=_payload(*cards, commit=True),
        ).json()
        assert result["added"] == 8
        assert result["error_count"] == 0
        model_names = client.portal.call(
            client.app.state.service.run,
            lambda col: {
                col.get_note(note_id).note_type()["name"]
                for note_id in col.find_notes('deck:"Languages::Spanish"')
            },
        )
        assert model_names == {spec.model_name for spec in CARD_TYPES.values()}


def test_incompatible_existing_note_type_is_never_rewritten_automatically(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "bad-model.anki2")
    with _client(settings) as client:
        def seed_bad_model(col):
            col.decks.id("Languages::Spanish")
            col.decks.id("Languages::English")
            model = col.models.new("Vocabulary Production")
            col.models.add_field(model, col.models.new_field("Prompt"))
            template = col.models.new_template("Card")
            template["qfmt"] = "{{Prompt}}"
            template["afmt"] = "{{FrontSide}}"
            col.models.add_template(model, template)
            col.models.add_dict(model)

        client.portal.call(client.app.state.service.run, seed_bad_model)
        result = client.post(
            "/api/lesson-cards/batch", json=_payload(_vocabulary(), commit=True),
        ).json()
        assert result["added"] == 0
        assert result["error_count"] == 1
        assert "missing fields" in result["errors"][0]["message"]
        fields = client.portal.call(
            client.app.state.service.run,
            lambda col: [field["name"] for field in col.models.by_name("Vocabulary Production")["flds"]],
        )
        assert fields == ["Prompt"]
