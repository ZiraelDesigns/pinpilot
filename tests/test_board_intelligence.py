import json
from datetime import datetime

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal, engine
from app.models import (
    PinCreative,
    PinterestAccount,
    PinterestBoard,
    PinterestBoardRecommendation,
    PinterestBoardSEOProfile,
    Product,
    SEOGeneration,
    SEOKeywordIntelligence,
    SEOQualityAssessment,
)
from app.models.core import PinCreativeType
from app.services.board_intelligence import (
    BOARD_MATCH_VERSION,
    BOARD_METADATA_VERSION,
    BOARD_SEO_VERSION,
    BOARD_SOURCE_API,
    analyze_board_metadata,
    ensure_board_seo_profile,
    publisher_board_candidates,
    recommend_boards_for_generation,
)
from app.services.ai_content import AIContentService
from app.services.keyword_intelligence import analyze_keyword_set
from app.services.pinterest import PinterestApiService
from app.services.seo_quality import (
    SEO_SCORE_VERSION,
    SEO_VALIDATION_VERSION,
    calculate_seo_quality,
)


def _seo_metadata(*, intent="aesthetic_style_intent"):
    return {
        "primary_keyword": "botanical soy candle",
        "secondary_keywords": ["natural wax candle", "home decor"],
        "long_tail_keywords": ["botanical soy candle for a calm reading room"],
        "audience_keywords": ["shoppers"],
        "use_case_keywords": ["reading room"],
        "search_intents": [intent],
        "creative_angle": "botanical reading room decor",
    }


def _add_generation(db, *, intent="aesthetic_style_intent", with_intelligence=True):
    seo = _seo_metadata(intent=intent)
    product = Product(title="Botanical Soy Candle", description="Natural wax candle for a reading room")
    db.add(product)
    db.flush()
    snapshot = {
        "title": "Botanical Soy Candle for a Calm Reading Room",
        "description": "A botanical soy candle and natural wax candle for calm reading room decor.",
        "seo_metadata": seo,
        # These must not enter board metadata or matching evidence.
        "image_path": "/private/local/image.png",
        "access_token": "board-test-private-token",
    }
    generation = SEOGeneration(
        product_id=product.id,
        started_at=datetime(2026, 9, 30),
        completed_at=datetime(2026, 9, 30),
        provider="mock",
        model_name="mock-v1",
        prompt_version="seo-prompt-v2",
        schema_version="pinterest-seo-v2",
        status="completed",
        output_snapshot=snapshot,
    )
    db.add(generation)
    db.flush()
    if with_intelligence:
        intel = analyze_keyword_set(
            seo,
            title=snapshot["title"],
            description=snapshot["description"],
            product_tags=["candle", "home decor"],
        )
        db.add(SEOKeywordIntelligence(
            seo_generation_id=generation.id,
            computed_at=datetime(2026, 9, 30),
            algorithm_version="keyword_intelligence_v1",
            status=intel["status"],
            keyword_items=intel["keyword_items"],
            candidate_sets=intel["candidate_sets"],
            quality_summary=intel["quality_summary"],
            external_signals=intel["external_signals"],
            signal_origins=intel["signal_origins"],
        ))
        assessment_result = calculate_seo_quality(snapshot, intel)
        db.add(SEOQualityAssessment(
            seo_generation_id=generation.id,
            assessed_at=datetime(2026, 9, 30),
            score_version=SEO_SCORE_VERSION,
            validation_version=SEO_VALIDATION_VERSION,
            calculation_type="deterministic_heuristic",
            overall_score=assessment_result["score"]["overall"],
            score_breakdown=assessment_result["score"],
            validation_status=assessment_result["validation"]["status"],
            validation_result=assessment_result["validation"],
        ))
        db.flush()
    return generation


def _add_board(db, account, board_id, name, description=None, *, source="local_board_metadata"):
    board = PinterestBoard(
        account=account,
        board_id=board_id,
        name=name,
        description=description,
        source=source,
        fetched_at=datetime(2026, 9, 30),
        metadata_version=BOARD_METADATA_VERSION,
    )
    db.add(board)
    db.flush()
    return board


def test_board_metadata_normalization_extraction_intents_groups_and_unavailable_signals_are_deterministic():
    first = analyze_board_metadata(
        "  Home Decor Botanical & Soy Candle IDEAS ",
        "Natural wax candles for calm reading rooms and thoughtful gifts for shoppers.",
    )
    second = analyze_board_metadata(
        "  Home Decor Botanical & Soy Candle IDEAS ",
        "Natural wax candles for calm reading rooms and thoughtful gifts for shoppers.",
    )

    assert first == second
    assert first["algorithm_version"] == BOARD_SEO_VERSION
    assert first["calculation_type"] == "deterministic_computed"
    assert {"botanical", "soy", "candle", "home", "decor"} <= set(first["normalized_terms"])
    assert first["search_intents"] != ["unknown"]
    assert "home_decor" in first["topic_signals"]
    assert "shoppers" in first["audience_signals"]
    assert any(item["classification"] == "PRIMARY" for item in first["keyword_items"])
    assert all(item["raw_term"] and item["normalized_term"] for item in first["keyword_items"])
    assert all(item["semantic_group"] for item in first["keyword_items"] if item["valid"])
    assert all(item["source"] in {"board_name", "board_description", "board_name_and_description"}
               for item in first["keyword_items"])
    assert all(signal == {"status": "not_collected", "value": None, "source": None}
               for signal in first["external_signals"].values())


def test_board_name_only_and_missing_description_do_not_invent_board_metadata():
    result = analyze_board_metadata("Studio Ceramics", None)
    assert result["input_snapshot"] == {"name": "Studio Ceramics", "description": ""}
    assert result["source"] == "local_board_metadata"
    assert result["external_signals"]["trend"]["status"] == "not_collected"


def test_board_sync_is_account_scoped_and_persists_only_mocked_api_metadata():
    class FakeBoards:
        def list_boards(self):
            return [{
                "id": "shared-external-id",
                "name": "Botanical Soy Candles",
                "description": "Natural wax candle ideas for shoppers",
                "privacy": "PUBLIC",
            }]

    with SessionLocal() as db:
        first_account = PinterestAccount(account_name="First", account_identifier="first-account", is_active=True)
        second_account = PinterestAccount(account_name="Second", account_identifier="second-account", is_active=True)
        db.add_all([first_account, second_account])
        db.flush()
        service = PinterestApiService(db, first_account, api_client=FakeBoards())
        assert service.sync_boards() == 1
        assert service.sync_boards() == 1
        first_board = db.query(PinterestBoard).filter_by(
            account_id=first_account.id, board_id="shared-external-id"
        ).one()
        first_profile_count = db.query(PinterestBoardSEOProfile).filter_by(board_id=first_board.id).count()
        second_board = _add_board(
            db,
            second_account,
            "shared-external-id",
            "Other account board",
            "Independent metadata",
        )
        db.commit()

        assert first_board.source == BOARD_SOURCE_API
        assert first_board.metadata_version == BOARD_METADATA_VERSION
        assert first_board.fetched_at is not None
        assert first_profile_count == 1
        assert first_board.id != second_board.id
        assert db.query(PinterestBoard).filter_by(board_id="shared-external-id").count() == 2


def test_board_refresh_creates_new_immutable_profile_and_reuses_unchanged_profile():
    with SessionLocal() as db:
        account = PinterestAccount(account_name="Refresh", account_identifier="refresh-account", is_active=True)
        db.add(account)
        db.flush()
        board = _add_board(db, account, "refresh-board", "Garden Decor", "Ideas for a small garden")
        old_profile = ensure_board_seo_profile(db, board)
        assert ensure_board_seo_profile(db, board).id == old_profile.id
        board.name = "Garden Decor and Patio Ideas"
        board.description = "Plan a small garden patio"
        board.fetched_at = datetime(2026, 10, 1)
        board.source = BOARD_SOURCE_API
        db.flush()
        new_profile = ensure_board_seo_profile(db, board)
        db.commit()

        assert new_profile.id != old_profile.id
        assert old_profile.input_snapshot["name"] == "Garden Decor"
        assert new_profile.input_snapshot["name"] == "Garden Decor and Patio Ideas"
        assert db.query(PinterestBoardSEOProfile).filter_by(board_id=board.id).count() == 2


def test_matching_uses_keyword_intent_audience_use_case_and_topic_evidence_with_breakdown():
    with SessionLocal() as db:
        account = PinterestAccount(account_name="SEO boards", account_identifier="seo-boards", is_active=True)
        db.add(account)
        db.flush()
        generation = _add_generation(db)
        strong = _add_board(
            db,
            account,
            "botanical-board",
            "Botanical Soy Candle Decor",
            "Natural wax candle decor for shoppers and calm reading room gifts.",
        )
        weak = _add_board(
            db,
            account,
            "fitness-board",
            "Gym Workout Fitness",
            "Fitness workout ideas for runners.",
        )
        results = recommend_boards_for_generation(db, generation.id, account_id=account.id)
        db.commit()

        assert results[0].board_id == strong.id
        assert results[0].status == "recommended"
        assert results[0].algorithm_version == BOARD_MATCH_VERSION
        assert results[0].source == "local_deterministic_match"
        assert results[0].match_breakdown["score_origin"] == "computed_heuristic_not_pinterest_ranking"
        assert set(results[0].match_breakdown["components"]) == {
            "keyword_overlap", "semantic_similarity", "intent_compatibility",
            "audience_compatibility", "use_case_compatibility", "topic_relevance",
        }
        assert results[0].match_breakdown["components"]["intent_compatibility"]["score"] == 100
        assert results[0].match_breakdown["components"]["audience_compatibility"]["score"] > 0
        assert results[0].match_breakdown["components"]["use_case_compatibility"]["score"] > 0
        assert results[0].match_breakdown["components"]["topic_relevance"]["score"] > 0
        assert "compatible_search_intent" in results[0].positive_signals
        assert results[0].match_breakdown["evidence"]["shared_keyword_phrases"]
        weak_result = next(item for item in results if item.board_id == weak.id)
        assert weak_result.negative_signals
        assert "search_intent_mismatch" in weak_result.negative_signals
        assert weak_result.match_score < results[0].match_score
        assert generation.quality_assessment is not None


def test_ai_generation_automatically_persists_the_seo_to_keyword_quality_board_match_chain():
    class MockSEOProvider:
        provider_name = "board-chain-test-provider"
        model_name = "mock-seo-only"

        def generate_json(self, _prompt):
            return json.dumps({
                "title": "Botanical Soy Candle for a Calm Reading Room",
                "description": "A botanical soy candle and natural wax candle for calm reading room decor.",
                "call_to_action": "Explore product details",
                "seo": _seo_metadata(intent="product_search"),
            })

    with SessionLocal() as db:
        account = PinterestAccount(account_name="Chain", account_identifier="chain-account", is_active=True)
        product = Product(title="Botanical Soy Candle", description="Natural wax candle for a reading room")
        db.add_all([account, product])
        db.flush()
        board = _add_board(
            db, account, "chain-board", "Botanical Soy Candle Decor", "Natural wax candle reading room decor"
        )
        creative = AIContentService(db, MockSEOProvider()).generate(
            product, PinCreativeType.PRODUCT_FOCUS, 1
        )[0]
        db.commit()

        generation = db.query(SEOGeneration).filter_by(creative_id=creative.id).one()
        intelligence = db.query(SEOKeywordIntelligence).filter_by(seo_generation_id=generation.id).one()
        assessment = db.query(SEOQualityAssessment).filter_by(seo_generation_id=generation.id).one()
        recommendation = db.query(PinterestBoardRecommendation).filter_by(
            seo_generation_id=generation.id,
            board_id=board.id,
        ).one()
        profile = db.get(PinterestBoardSEOProfile, recommendation.board_profile_id)

        assert db.get(PinCreative, creative.id) is not None
        assert intelligence.id is not None
        assert assessment.id is not None
        assert profile.board_id == board.id
        assert recommendation.seo_generation_id == generation.id
        assert recommendation.match_score > 0


def test_matching_is_cached_deterministic_and_ties_have_stable_account_board_order():
    with SessionLocal() as db:
        accounts = [
            PinterestAccount(account_name=f"A{i}", account_identifier=f"tie-account-{i}", is_active=True)
            for i in (1, 2)
        ]
        db.add_all(accounts)
        db.flush()
        generation = _add_generation(db)
        _add_board(db, accounts[1], "same-board", "Botanical Soy Candle Decor", "Natural wax candle decor")
        _add_board(db, accounts[0], "same-board", "Botanical Soy Candle Decor", "Natural wax candle decor")
        first = recommend_boards_for_generation(db, generation.id, top_n=1)
        db.commit()
        second = recommend_boards_for_generation(db, generation.id, top_n=1)
        db.commit()

        assert [item.id for item in first] == [item.id for item in second]
        assert [item.rank for item in first] == list(range(1, len(first) + 1))
        assert first[0].board.account_id == min(account.id for account in accounts)
        assert first[0].status == "recommended"
        assert all(item.status == "candidate" for item in first[1:])
        assert len(first) == db.query(PinterestBoardRecommendation).filter_by(
            seo_generation_id=generation.id
        ).count()


def test_bulk_board_matching_queries_do_not_grow_one_select_per_board():
    with SessionLocal() as db:
        account = PinterestAccount(account_name="Bulk", account_identifier="bulk-board-account", is_active=True)
        db.add(account)
        db.flush()
        generation = _add_generation(db)
        for index in range(12):
            _add_board(
                db,
                account,
                f"bulk-{index:02d}",
                f"Botanical Soy Candle Decor {index}",
                "Natural wax candle and calm reading room decor",
            )
        db.commit()

        statements = []

        def count_selects(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", count_selects)
        try:
            results = recommend_boards_for_generation(db, generation.id, account_id=account.id)
            db.commit()
        finally:
            event.remove(engine, "before_cursor_execute", count_selects)

        assert len(results) == 12
        assert len(statements) < 12


def test_same_external_board_id_duplicate_within_one_account_is_rejected():
    with SessionLocal() as db:
        account = PinterestAccount(account_name="Unique", account_identifier="unique-account", is_active=True)
        db.add(account)
        db.flush()
        _add_board(db, account, "same-id", "Board one")
        db.add(PinterestBoard(account=account, board_id="same-id", name="Board duplicate"))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        else:
            raise AssertionError("Board external ID must be unique within one Pinterest account")


def test_legacy_generation_without_keyword_intelligence_is_not_fabricated_or_matched():
    with SessionLocal() as db:
        account = PinterestAccount(account_name="Legacy", account_identifier="legacy-board-account", is_active=True)
        db.add(account)
        db.flush()
        generation = _add_generation(db, with_intelligence=False)
        board = _add_board(db, account, "legacy-board", "Botanical Candle Board")
        assert recommend_boards_for_generation(db, generation.id) == []
        assert db.query(PinterestBoardSEOProfile).filter_by(board_id=board.id).count() == 0
        assert db.query(PinterestBoardRecommendation).count() == 0


def test_publisher_interface_reads_only_persisted_account_scoped_candidates():
    with SessionLocal() as db:
        first_account = PinterestAccount(account_name="One", account_identifier="scope-one", is_active=True)
        second_account = PinterestAccount(account_name="Two", account_identifier="scope-two", is_active=True)
        db.add_all([first_account, second_account])
        db.flush()
        generation = _add_generation(db)
        _add_board(db, first_account, "a-board", "Botanical Soy Candle Decor", "Natural wax candles")
        _add_board(db, second_account, "b-board", "Botanical Soy Candle Decor", "Natural wax candles")
        assert publisher_board_candidates(db, generation.id, first_account.id) == []
        recommend_boards_for_generation(db, generation.id, account_id=first_account.id)
        db.commit()
        candidates = publisher_board_candidates(db, generation.id, first_account.id)

        assert len(candidates) == 1
        assert candidates[0].board.account_id == first_account.id
        assert not publisher_board_candidates(db, generation.id, second_account.id)


def test_board_profiles_and_matches_do_not_persist_pin_secrets_or_local_image_paths():
    with SessionLocal() as db:
        account = PinterestAccount(account_name="Safe", account_identifier="safe-board-account", is_active=True)
        db.add(account)
        db.flush()
        generation = _add_generation(db)
        board = _add_board(db, account, "safe-board", "Botanical Soy Candle", "Natural wax candle decor")
        profile = ensure_board_seo_profile(db, board)
        recommendations = recommend_boards_for_generation(db, generation.id)
        db.commit()
        persisted = str(profile.input_snapshot) + str(profile.keyword_items) + str(recommendations[0].match_breakdown)

        assert "board-test-private-token" not in persisted
        assert "/private/local/image.png" not in persisted
        assert "access_token" not in persisted
        assert "image_path" not in persisted
