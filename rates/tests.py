from django.test import TestCase

# Create your tests here.


class FillFundingTests(TestCase):
    def test_from_reloads_series_and_only_limits_fields(self):
        from datetime import date
        from decimal import Decimal
        from io import StringIO
        from unittest import mock

        from django.core.management import call_command

        from rates.models import FundingDay

        FundingDay.objects.create(date=date(2025, 6, 2), swaprate_usd=Decimal("10"), source="moex")
        calls = []

        def fake(secid, start, end, lot):
            calls.append((secid, start))
            return [(date(2023, 4, 11), Decimal("5")), (date(2025, 6, 2), Decimal("11"))]

        with mock.patch("rates.funding.fetch_swap", side_effect=fake):
            out = StringIO()
            call_command("sync_funding", "--from", "2023-01-01", "--only", "usd", stdout=out)
        # Только USD и с указанной даты, а не с дня после последней сохранённой.
        self.assertEqual(calls, [("USDRUBF", date(2023, 1, 1))])
        self.assertEqual(FundingDay.objects.get(date=date(2023, 4, 11)).swaprate_usd, Decimal("5"))
        self.assertEqual(FundingDay.objects.get(date=date(2025, 6, 2)).swaprate_usd, Decimal("11"))
        self.assertIn("USD: 2023-04-11 → 2025-06-02, дней 2", out.getvalue())
