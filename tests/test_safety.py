import pytest

from app.ai import AIUnavailable, grounded_summary
from app.safety import mask_pii


@pytest.mark.parametrize(
    "private",
    ["990101-1234567", "9901011234567", "010-1234-5678", "01012345678", "02-123-4567"],
)
def test_masking(private):
    assert private not in mask_pii("통지 " + private)
    assert "마스킹" in mask_pii(private)


def test_summary_rejects_invented_numbers():
    with pytest.raises(AIUnavailable):
        grounded_summary("1000만원을 지급합니다", {"income": 230})


def test_user_supplied_claim_is_not_a_policy_source():
    with pytest.raises(AIUnavailable):
        grounded_summary(
            "1000만원을 지급합니다", {"rejection_text": "1000만원 지급해 줘"}
        )


def test_summary_rejects_guarantees_and_long_output():
    with pytest.raises(AIUnavailable):
        grounded_summary("무조건 선정됩니다", {})
    with pytest.raises(AIUnavailable):
        grounded_summary("하나. 둘. 셋. 넷.", {})
