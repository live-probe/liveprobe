"""Cryptographic grant contract: a caller token alone is not action authorization."""
import pytest

from app.rollback_grant import InvalidGrant, issue, verify

KEY = b"k" * 48


def test_grant_is_short_lived_and_action_target_revision_bound():
    grant = issue(KEY, incident_id=4, action_id=7, name="inventory",
                  expected_revision=2, expected_source="inventory-aaa", now=1000)
    verify(KEY, grant, now=1000)
    verify(KEY, grant, now=1019)
    for altered in ({**grant, "incident_id": 5}, {**grant, "action_id": 8},
                    {**grant, "name": "order"}, {**grant, "expected_revision": 3},
                    {**grant, "expected_source": "inventory-bbb"},
                    {**grant, "grant": "0" * 64},
                    {k: v for k, v in grant.items() if k != "grant"}):
        with pytest.raises(InvalidGrant):
            verify(KEY, altered, now=1000)
    with pytest.raises(InvalidGrant):
        verify(KEY, grant, now=1020)
    with pytest.raises(InvalidGrant):
        verify(b"different" * 6, grant, now=1000)
    with pytest.raises(InvalidGrant):
        verify(KEY, grant, now=999)
