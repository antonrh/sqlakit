"""What a test looks like once the plugin is on."""

import pytest
from app import User, rename

from sqlakit import MissingSessionError


@pytest.mark.db
def test_renaming_a_user() -> None:
    user = User(name="ada").save()

    rename(user.id, "grace")
    user.refresh()

    assert user.name == "grace"


@pytest.mark.db
def test_the_last_test_rolled_back() -> None:
    assert User.query.count() == 0


def test_without_the_marker_there_is_no_database() -> None:
    with pytest.raises(MissingSessionError):
        User.query.count()
