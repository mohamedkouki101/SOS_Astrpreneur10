"""Guardrail: duplicate / replayed / badly signed webhooks.

The signed webhook receiver is build-plan task 4 (not implemented yet). This test is kept as a
visible reminder so the guardrail is not forgotten.
"""

import pytest


@pytest.mark.skip(reason="webhook receiver not built yet (build-plan task 4)")
def test_duplicate_webhook_processed_once():
    raise AssertionError
