"""The generated narrative must come back in English.

The model that produces document analyses is free to answer in whatever
language it likes, and a Chinese-trained one periodically does: an English
treasury policy came back as a wholly Chinese summary and set of audit notes,
from the same model and prompt that had analysed the eight documents beside it
in English. Pinning the language in the prompt lowered the rate but did not
reach zero, so the durable guard is this validation — a rejected proposal is
handed back to the worker's existing repair turn with the reason.
"""

from __future__ import annotations

import pytest

from app import document_analysis

ENGLISH_SUMMARY = "## Summary\n\nDealing is permitted between 09:00 and 17:00. [c1]"
ENGLISH_NOTES = "## Observations\n\nThe review date is not stated. [c1]"
CHINESE_SUMMARY = "# 文档概述\n\n本摘录为《资金和投资政策》的一部分，文档参考TP/2023/04。[c1]"
CHINESE_NOTES = "# 审阅观察\n\n政策目的和适用范围未明确，建议获取完整政策文档。[c1]"


def test_english_narrative_passes_through_unchanged():
    output = document_analysis.validate_analysis_text(
        {"summary_markdown": ENGLISH_SUMMARY, "audit_notes_markdown": ENGLISH_NOTES}
    )
    assert output == {
        "summary_markdown": ENGLISH_SUMMARY,
        "audit_notes_markdown": ENGLISH_NOTES,
    }


@pytest.mark.parametrize(
    "payload, field",
    [
        ({"summary_markdown": CHINESE_SUMMARY, "audit_notes_markdown": ENGLISH_NOTES},
         "summary_markdown"),
        ({"summary_markdown": ENGLISH_SUMMARY, "audit_notes_markdown": CHINESE_NOTES},
         "audit_notes_markdown"),
    ],
)
def test_a_narrative_written_in_another_script_is_rejected(payload, field):
    # Either field alone is enough: a memo whose notes drifted is as unusable to
    # the reviewer as one whose summary did.
    with pytest.raises(ValueError) as caught:
        document_analysis.validate_analysis_text(payload)
    message = str(caught.value)
    assert field in message
    # The message is what reaches the repair turn, so it has to say what to do.
    assert "English" in message


def test_english_prose_may_still_name_a_counterparty_in_its_own_script():
    # The rejection is for prose that has drifted wholesale, not for borrowing.
    # An auditor naming the counterparty as it appears on the confirmation is
    # writing English, and must not be sent round the repair loop for it.
    summary = "## Summary\n\nThe deal confirms a trade with 中国银行 for USD 5m. [c1]"
    output = document_analysis.validate_analysis_text(
        {"summary_markdown": summary, "audit_notes_markdown": ENGLISH_NOTES}
    )
    assert output["summary_markdown"] == summary


def test_the_blank_field_check_still_runs_first():
    # A blank field has no letters to measure, so the language check must not
    # shadow the more specific error the caller already relied on.
    with pytest.raises(ValueError, match="blank"):
        document_analysis.validate_analysis_text(
            {"summary_markdown": "", "audit_notes_markdown": CHINESE_NOTES}
        )
