import pytest
from uuid import uuid4

from saas_core.analytics.service import repeated_paths


@pytest.mark.parametrize("kwargs", [
    {"days": 0},
    {"days": 731},
    {"max_steps": 1},
    {"max_steps": 6},
    {"max_steps": True},
    {"min_repetitions": 1},
    {"min_repetitions": 10_001},
    {"min_repetitions": True},
    {"limit": 0},
    {"limit": 101},
    {"limit": True},
])
def test_repeated_path_report_rejects_unbounded_or_invalid_parameters(kwargs):
    with pytest.raises(ValueError):
        repeated_paths(None, **kwargs)


def test_user_level_path_report_requires_auditing_actor():
    with pytest.raises(ValueError, match="actor_user_id is required"):
        repeated_paths(None, user_id=uuid4())


def test_user_level_path_report_rejects_non_uuid_target():
    with pytest.raises(ValueError, match="user_id must be a UUID"):
        repeated_paths(None, user_id="not-a-uuid", actor_user_id=uuid4())
