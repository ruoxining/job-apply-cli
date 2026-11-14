import json

import pytest
from sqlalchemy import select

from job_apply_agent.domain.schemas import CandidateProfile, ProfileFact
from job_apply_agent.experience.library import Experience, load_experiences, save_experience
from job_apply_agent.experience.retrieval import HybridRetriever, VectorCache, lexical_tokens
from job_apply_agent.models import ExperienceRevisionRecord


class TestEncoder:
    """Deterministic fixture for ranking/cache logic; NOT a semantic benchmark."""

    __test__ = False
    identity = "fixture-v1"

    def __init__(self):
        self.encoded = []

    def chunks(self, text):
        return [text]

    def encode(self, texts):
        self.encoded.extend(texts)
        return [[1.0, 0.01] if "database" in t else [0.01, 1.0] for t in texts]


def item(identifier, *descriptions, **kwargs):
    return Experience(id=identifier, description=list(descriptions), source="test", **kwargs)


def test_update_idempotence_history_and_content_addressed_invalidation(session, tmp_path):
    old = item("db", "database replication", "SQL diagnostics")
    save_experience(session, old, event_key="1")
    session.commit()
    new = item("db", "database failover", "SQL diagnostics")
    assert save_experience(session, new, event_key="2").revision == 2
    assert save_experience(session, old, event_key="2").revision == 2
    session.commit()
    history = list(session.scalars(select(ExperienceRevisionRecord)))
    assert len(history) == 2 and history[0].payload["description"] == old.description
    assert (
        load_experiences(CandidateProfile(), tmp_path / "absent", session)[0].description
        == new.description
    )
    encoder = TestEncoder()
    cache = VectorCache(tmp_path / "vectors.sqlite3")
    assert HybridRetriever([old], encoder, cache).new_vectors == 2
    assert HybridRetriever([new], encoder, cache).new_vectors == 1
    encoder.identity = "fixture-v2"
    assert HybridRetriever([new], encoder, cache).new_vectors == 2


def test_same_content_does_not_bump_revision(session):
    saved = item("job", "wrote tests")
    save_experience(session, saved, event_key="1")
    assert save_experience(session, saved, event_key="2").revision == 1


def test_canonical_descriptions_override_short_profile_and_db_overrides_canonical(
    session, tmp_path
):
    path = tmp_path / "library.json"
    path.write_text(
        json.dumps(
            {
                "projects": [
                    {"id": "db", "description": ["full description"], "start_year": "2024"}
                ],
                "skills": ["Python"],
            }
        )
    )
    profile = CandidateProfile(facts=[ProfileFact(id="db", statement="short", source="old")])
    library = {x.id: x for x in load_experiences(profile, path, session)}
    assert library["db"].description == ["full description"]
    assert library["db"].metadata["start_year"] == "2024"
    assert library["candidate-skills"].kind == "skills"
    save_experience(session, item("db", "updated"), event_key="new")
    assert load_experiences(profile, path, session)[0].description == ["updated"]


def test_description_hit_returns_whole_experience_and_deduplicates(tmp_path):
    experiences = [
        item("db", "database replication", "taught SQL", metadata={"end_year": "2024"}),
        item("ai", "NLP research"),
    ]
    index = HybridRetriever(experiences, TestEncoder(), VectorCache(tmp_path / "cache"))
    result = index.retrieve("database reliability", top_k=1)
    assert result.experiences == experiences[:1]
    assert len(result.experiences[0].description) == 2
    assert result.matches[0]["description_index"] == 0
    assert result.experiences[0].metadata["end_year"] == "2024"


@pytest.mark.parametrize(
    "query",
    [
        "Current company",
        "Total years of experience",
        "When did you graduate?",
        "Have you ever worked at X?",
        "目前是否在职？",
        "2024 年的经历",
    ],
)
def test_coverage_questions_include_all_intervals(tmp_path, query):
    experiences = [item("db", "database"), item("ai", "NLP")]
    index = HybridRetriever(experiences, TestEncoder(), VectorCache(tmp_path / "cache"))
    assert index.retrieve(query, top_k=1).experiences == experiences
    assert index.retrieve(query).route == "coverage_full"


def test_keyword_dates_and_chinese():
    assert {"2024", "sql", "语言"} <= set(lexical_tokens("2024 SQL 自然语言"))


def test_empty_corpus(tmp_path):
    index = HybridRetriever([], TestEncoder(), VectorCache(tmp_path / "cache"))
    assert index.retrieve("Python").experiences == []


def test_corrupt_vectors_not_cached(tmp_path):
    encoder = TestEncoder()
    encoder.encode = lambda texts: [[float("nan"), 1] for t in texts]
    with pytest.raises(ValueError, match="invalid embedding"):
        HybridRetriever([item("a", "bad")], encoder, VectorCache(tmp_path / "cache"))
