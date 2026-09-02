"""Send one hand-written report query to a running debug server.

```console
$ uv run sqlakit debugserver &
$ uv run python debugserver_wide.py
```

Thirty columns, two joins and fifty ids: the shape the compact layout has to
break up. The SQL is sent as it is, without a database to run it on.
"""

from __future__ import annotations

import argparse
import uuid

from sqlakit import DebugServer, Recording, Statement
from sqlakit._debugserver import flush_recordings, send_recording

COLUMNS = [
    "email_id",
    "stats.email_type",
    "email_id as campaign_id",
    "sends",
    "delivered",
    "opens",
    "clicks",
    "unique_opens",
    "unique_clicks",
    "unique_bounces as bounces",
    "unique_spam_complaints as spam_complaints",
    "unsubscribes",
    "open_rate",
    "delivered_rate",
    "click_through_rate",
    "click_to_open_rate",
    "bounce_rate",
    "spam_complaints_rate",
    "unsubscribe_rate",
    "doi_confirms",
    "doi_confirmation_rate",
    "unique_clicks_dropoff",
    "unique_opens_dropoff",
    "cast(bounce_rate - avg_bounce_rate as decimal(20, 3)) as bounce_rate_change",
    "cast( spam_complaints_rate - avg_spam_complaints_rate as decimal(20, 3) ) "
    "as spam_complaints_rate_change",
    "cast( unsubscribe_rate - avg_unsubscribe_rate as decimal(20, 3) ) "
    "as unsubscribe_rate_change",
    "cast(open_rate - avg_open_rate as decimal(20, 3)) as open_rate_change",
    "cast( delivered_rate - avg_delivered_rate as decimal(20, 3) ) "
    "as delivered_rate_change",
    "cast( (unique_clicks::decimal(20, 3) / nullif(avg_unique_clicks, 0)"
    "::decimal(20, 3)) - 1 as decimal(20, 3) ) as unique_clicks_change",
    "cast( click_through_rate - avg_click_through_rate as decimal(20, 3) ) "
    "as click_through_rate_change",
    "cast( click_to_open_rate - avg_click_to_open_rate as decimal(20, 3) ) "
    "as click_to_open_rate_change",
]

JOINS = """from v_email_analytics_v3_dbt stats
left join email_campaign email on stats.email_id = email.id
left join email_analytics_benchmark_by_account_artist_v2_dbt ben
on coalesce(email.global_participant_id, 'null') = coalesce(ben.global_participant_id, 'null')
and coalesce(email.custom_list_id, 'null') = coalesce(ben.custom_list_id, 'null')
and email.vendor_id = ben.vendor_id and email.subaccount_id = ben.subaccount_id
and stats.email_type = ben.email_type"""


def wide(ids: int) -> str:
    """Return the query, with a comment naming the template it came from."""
    listed = " , ".join(f"'{uuid.uuid4()}'" for _ in range(ids))
    return (
        "/* email-analytics/find-by-email-ids.sql */\n"
        f"select {', '.join(COLUMNS)} {JOINS} "
        f"where email_id in ({listed} ) and stats.email_type = 'CAMPAIGN'"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-H", "--host", default="localhost")
    parser.add_argument("-p", "--port", type=int, default=5555)
    parser.add_argument("--ids", type=int, default=50, help="how many in the IN list")
    options = parser.parse_args()

    recording = Recording(
        label="GET /analytics",
        statements=[
            Statement(
                sql=wide(options.ids),
                parameters=None,
                duration=0.042,
                dialect="postgresql",
                stack=["/app/reports/email_analytics.py:88 in by_email_ids"],
            ),
            Statement(
                sql="SELECT users.id AS users_id, users.name AS users_name FROM users "
                "WHERE users.id = %(id_1)s",
                parameters={"id_1": 7},
                duration=0.0004,
                dialect="postgresql",
                stack=["/app/reports/email_analytics.py:91 in by_email_ids"],
            ),
        ],
    )
    server = DebugServer(options.host, options.port, app="reports", tags=("analytics",))
    send_recording(recording, server)
    flush_recordings(timeout=2.0)
    print(f"sent to http://{options.host}:{options.port}")


if __name__ == "__main__":
    main()
