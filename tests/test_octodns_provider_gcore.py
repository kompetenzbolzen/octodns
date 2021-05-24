#
#
#

from __future__ import (
    absolute_import,
    division,
    print_function,
    unicode_literals,
)

from mock import Mock, call
from os.path import dirname, join
from requests_mock import ANY, mock as requests_mock
from six import text_type
from unittest import TestCase

from octodns.record import Record, Update, Delete
from octodns.provider.gcore import (
    GCoreProvider,
    GCoreClientBadRequest,
    GCoreClientNotFound,
    GCoreClientException,
)
from octodns.provider.yaml import YamlProvider
from octodns.zone import Zone


class TestGCoreProvider(TestCase):
    expected = Zone("unit.tests.", [])
    source = YamlProvider("test", join(dirname(__file__), "config"))
    source.populate(expected)

    def test_populate(self):

        provider = GCoreProvider("test_id", token="token")

        # 400 - Bad Request.
        with requests_mock() as mock:
            mock.get(ANY, status_code=400, text='{"error":"bad body"}')

            with self.assertRaises(GCoreClientBadRequest) as ctx:
                zone = Zone("unit.tests.", [])
                provider.populate(zone)
            self.assertIn('"error":"bad body"', text_type(ctx.exception))

        # 404 - Not Found.
        with requests_mock() as mock:
            mock.get(
                ANY, status_code=404, text='{"error":"zone is not found"}'
            )

            with self.assertRaises(GCoreClientNotFound) as ctx:
                zone = Zone("unit.tests.", [])
                provider._client.zone(zone.name)
            self.assertIn(
                '"error":"zone is not found"', text_type(ctx.exception)
            )

        # General error
        with requests_mock() as mock:
            mock.get(ANY, status_code=500, text="Things caught fire")

            with self.assertRaises(GCoreClientException) as ctx:
                zone = Zone("unit.tests.", [])
                provider.populate(zone)
            self.assertEquals("Things caught fire", text_type(ctx.exception))

        # No credentials or token error
        with requests_mock() as mock:
            with self.assertRaises(ValueError) as ctx:
                GCoreProvider("test_id")
            self.assertEquals(
                "either token or login & password must be set",
                text_type(ctx.exception),
            )

        # Auth with login password
        with requests_mock() as mock:

            def match_body(request):
                return {"username": "foo", "password": "bar"} == request.json()

            auth_url = "http://api/auth/jwt/login"
            mock.post(
                auth_url,
                additional_matcher=match_body,
                status_code=200,
                json={"access": "access"},
            )

            providerPassword = GCoreProvider(
                "test_id",
                url="http://dns",
                auth_url="http://api",
                login="foo",
                password="bar",
            )
            assert mock.called

            # make sure token passed in header
            zone_rrset_url = "http://dns/zones/unit.tests/rrsets?all=true"
            mock.get(
                zone_rrset_url,
                request_headers={"Authorization": "Bearer access"},
                status_code=404,
            )
            zone = Zone("unit.tests.", [])
            assert not providerPassword.populate(zone)

        # No diffs == no changes
        with requests_mock() as mock:
            base = "https://dnsapi.gcorelabs.com/v2/zones/unit.tests/rrsets"
            with open("tests/fixtures/gcore-no-changes.json") as fh:
                mock.get(base, text=fh.read())

            zone = Zone("unit.tests.", [])
            provider.populate(zone)
            self.assertEquals(4, len(zone.records))
            self.assertEquals(
                {"aaaa", "www", "www.sub", ""}, {r.name for r in zone.records}
            )
            changes = self.expected.changes(zone, provider)
            self.assertEquals(0, len(changes))

        # 3 removed + 1 modified
        with requests_mock() as mock:
            base = "https://dnsapi.gcorelabs.com/v2/zones/unit.tests/rrsets"
            with open("tests/fixtures/gcore-records.json") as fh:
                mock.get(base, text=fh.read())

            zone = Zone("unit.tests.", [])
            provider.populate(zone)
            self.assertEquals(1, len(zone.records))
            changes = self.expected.changes(zone, provider)
            self.assertEquals(4, len(changes))
            self.assertEquals(
                3, len([c for c in changes if isinstance(c, Delete)])
            )
            self.assertEquals(
                1, len([c for c in changes if isinstance(c, Update)])
            )

    def test_apply(self):
        provider = GCoreProvider("test_id", url="http://api", token="token")

        # Zone does not exists but can be created.
        with requests_mock() as mock:
            mock.get(
                ANY, status_code=404, text='{"error":"zone is not found"}'
            )
            mock.post(ANY, status_code=200, text='{"id":1234}')

            plan = provider.plan(self.expected)
            provider.apply(plan)

        # Zone does not exists and can't be created.
        with requests_mock() as mock:
            mock.get(
                ANY, status_code=404, text='{"error":"zone is not found"}'
            )
            mock.post(
                ANY,
                status_code=400,
                text='{"error":"parent zone is already'
                ' occupied by another client"}',
            )

            with self.assertRaises(
                (GCoreClientNotFound, GCoreClientBadRequest)
            ) as ctx:
                plan = provider.plan(self.expected)
                provider.apply(plan)
            self.assertIn(
                "parent zone is already occupied by another client",
                text_type(ctx.exception),
            )

        resp = Mock()
        resp.json = Mock()
        provider._client._request = Mock(return_value=resp)

        with open("tests/fixtures/gcore-zone.json") as fh:
            zone = fh.read()

        # non-existent domain
        resp.json.side_effect = [
            GCoreClientNotFound(resp),  # no zone in populate
            GCoreClientNotFound(resp),  # no domain during apply
            zone,
        ]
        plan = provider.plan(self.expected)

        # create all
        self.assertEquals(4, len(plan.changes))
        self.assertEquals(4, provider.apply(plan))
        self.assertFalse(plan.exists)

        provider._client._request.assert_has_calls(
            [
                call(
                    "GET",
                    "http://api/zones/unit.tests/rrsets",
                    params={"all": "true"},
                ),
                call("GET", "http://api/zones/unit.tests"),
                call("POST", "http://api/zones", data={"name": "unit.tests"}),
                call(
                    "POST",
                    "http://api/zones/unit.tests/www.sub.unit.tests./A",
                    data={
                        "ttl": 300,
                        "resource_records": [{"content": ["2.2.3.6"]}],
                    },
                ),
                call(
                    "POST",
                    "http://api/zones/unit.tests/www.unit.tests./A",
                    data={
                        "ttl": 300,
                        "resource_records": [{"content": ["2.2.3.6"]}],
                    },
                ),
                call(
                    "POST",
                    "http://api/zones/unit.tests/aaaa.unit.tests./AAAA",
                    data={
                        "ttl": 600,
                        "resource_records": [
                            {
                                "content": [
                                    "2601:644:500:e210:62f8:1dff:feb8:947a"
                                ]
                            }
                        ],
                    },
                ),
                call(
                    "POST",
                    "http://api/zones/unit.tests/unit.tests./A",
                    data={
                        "ttl": 300,
                        "resource_records": [
                            {"content": ["1.2.3.4"]},
                            {"content": ["1.2.3.5"]},
                        ],
                    },
                ),
            ]
        )
        # expected number of total calls
        self.assertEquals(7, provider._client._request.call_count)

        provider._client._request.reset_mock()

        # delete 1 and update 1
        provider._client.zone_records = Mock(
            return_value=[
                {
                    "name": "www",
                    "ttl": 300,
                    "type": "A",
                    "resource_records": [{"content": ["1.2.3.4"]}],
                },
                {
                    "name": "ttl",
                    "ttl": 600,
                    "type": "A",
                    "resource_records": [{"content": ["3.2.3.4"]}],
                },
            ]
        )

        # Domain exists, we don't care about return
        resp.json.side_effect = ["{}"]

        wanted = Zone("unit.tests.", [])
        wanted.add_record(
            Record.new(
                wanted, "ttl", {"ttl": 300, "type": "A", "value": "3.2.3.4"}
            )
        )

        plan = provider.plan(wanted)
        self.assertTrue(plan.exists)
        self.assertEquals(2, len(plan.changes))
        self.assertEquals(2, provider.apply(plan))

        provider._client._request.assert_has_calls(
            [
                call(
                    "DELETE", "http://api/zones/unit.tests/www.unit.tests./A"
                ),
                call(
                    "PUT",
                    "http://api/zones/unit.tests/ttl.unit.tests./A",
                    data={
                        "ttl": 300,
                        "resource_records": [{"content": ["3.2.3.4"]}],
                    },
                ),
            ]
        )
