"""EnrichedRecord derivation from ActivityRecord."""

from __future__ import annotations

import unittest

from linkedin_api.activity_csv import ActivityRecord, ActivityType
from linkedin_api.enriched_record import EnrichedRecord


class EnrichedRecordActivityTypeTests(unittest.TestCase):
    def test_preserves_reaction_to_comment(self) -> None:
        rec = ActivityRecord(
            activity_type=ActivityType.REACTION_TO_COMMENT.value,
            reaction_type="LIKE",
            activity_urn="urn:li:comment:(activity:111,222)",
            post_id="111",
            time="1718784000000",
            activity_id="abc",
        )
        enriched = EnrichedRecord.from_activity_record(rec)
        self.assertEqual(enriched.interaction_type, "reaction")
        self.assertEqual(enriched.activity_type, "reaction_to_comment")
        self.assertEqual(enriched.reaction_type, "LIKE")

    def test_preserves_reaction_to_post(self) -> None:
        rec = ActivityRecord(
            activity_type=ActivityType.REACTION_TO_POST.value,
            reaction_type="PRAISE",
            activity_urn="urn:li:activity:111",
            post_id="111",
            time="1718784000000",
            activity_id="def",
        )
        enriched = EnrichedRecord.from_activity_record(rec)
        self.assertEqual(enriched.activity_type, "reaction_to_post")


if __name__ == "__main__":
    unittest.main()
