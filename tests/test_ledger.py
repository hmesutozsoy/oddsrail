"""The attribution ledger must subtract the maintainer honestly and bucket
weeks the way Polymarket's reward epochs do (Sunday 00:00 UTC)."""

from oddsrail import polymarket as pm, trading

MAINT = "0x69cd073d80d640b10818b0513e7237ac8688d48d"
OTHER = "0xabc0000000000000000000000000000000000001"

# 1788285747 = Tue 2026-09-01 18:02 UTC -> week starting Sun 2026-08-30
# 1788102773 = Sun 2026-08-30 15:12 UTC -> same week
# 1787900000 = Fri 2026-08-28 06:53 UTC -> week starting Sun 2026-08-23
ROWS = [
    {"maker": MAINT.upper(), "matchTime": "1788285747", "size": "920.21", "price": "0.78", "sizeUsdc": "717.76", "transactionHash": "0xaaa"},
    {"maker": MAINT, "matchTime": "1788102773", "size": "10", "price": "0.5"},
    {"maker": OTHER, "matchTime": "1788200000", "size": "100", "price": "0.25", "transactionHash": "0xbbb"},
    {"maker": OTHER, "matchTime": "1787900000", "size": "4", "price": "0.5"},
]


def test_week_bucketing_is_sunday_start_utc():
    assert pm._week_start_utc(1788285747) == "2026-08-30"   # Tuesday -> previous Sunday
    assert pm._week_start_utc(1788102773) == "2026-08-30"   # Sunday itself
    assert pm._week_start_utc(1787900000) == "2026-08-23"   # Friday -> its Sunday


def test_maintainer_is_subtracted_case_insensitively():
    led = pm.aggregate_ledger(ROWS, [MAINT], "0xcode")
    t = led["totals"]
    assert t["trades"] == 4 and t["wallets"] == 2
    assert t["external_wallets"] == 1
    assert t["external_volume_usd"] == 27.0          # 100*0.25 + 4*0.5
    assert t["volume_usd"] == 749.76                 # 717.76 (sizeUsdc wins) + 5 + 25 + 2
    wk = {w["week_start"]: w for w in led["weeks"]}
    assert wk["2026-08-30"]["external_wallets"] == 1 and wk["2026-08-30"]["external_volume_usd"] == 25.0
    assert wk["2026-08-30"]["maintainer_volume_usd"] == 722.76
    assert wk["2026-08-23"]["wallets"] == 1 and wk["2026-08-23"]["maintainer_volume_usd"] == 0.0
    assert led["weeks"][0]["week_start"] == "2026-08-30"   # newest first


def test_wallet_rows_carry_a_sample_tx_and_flags():
    led = pm.aggregate_ledger(ROWS, [MAINT])
    by = {w["wallet"]: w for w in led["wallets"]}
    assert by[MAINT]["is_maintainer"] is True and by[MAINT]["sample_tx"] == "0xaaa"
    assert by[OTHER]["is_maintainer"] is False and by[OTHER]["trades"] == 2


def test_no_maintainer_list_means_everyone_is_external():
    led = pm.aggregate_ledger(ROWS, [])
    assert led["totals"]["external_wallets"] == 2


def test_maintainer_wallets_env_and_override(monkeypatch):
    monkeypatch.delenv("ODDSRAIL_MAINTAINER_WALLETS", raising=False)
    monkeypatch.delenv("ODDSRAIL_BUILDER_CODE", raising=False)
    assert trading.maintainer_wallets() == [MAINT]
    monkeypatch.setenv("ODDSRAIL_BUILDER_CODE", "0x" + "1" * 64)
    assert trading.maintainer_wallets() == []            # someone else's code: our wallets do not apply
    monkeypatch.setenv("ODDSRAIL_MAINTAINER_WALLETS", " 0xAAA , 0xbbb ")
    assert trading.maintainer_wallets() == ["0xaaa", "0xbbb"]
