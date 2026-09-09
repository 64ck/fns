import pytest
from fastapi.testclient import TestClient

from fnsportal import analytics, demo
from fnsportal.api import create_app


@pytest.fixture()
def filled(conn):
    demo.generate(conn, year_from=2018, year_to=2020, seed=7)
    analytics.build_terms(conn, "tn", top_per_bucket=200)
    return conn


def test_profile_has_key_indicators_and_counts(filled):
    profile = analytics.profile(filled, "tn", 2020, "77")
    assert profile["region"]["name"] == "город Москва"
    assert profile["key_indicators"], "должны быть итоговые показатели разделов"
    assert profile["counts"]["rates"] > 0
    assert set(profile["benefits_by_payer"]) <= {"fl", "ip", "ul", "all"}


def test_series_adds_average_and_rank(filled):
    indicator = analytics.indicators(filled, "tn", 2020)[0]["id"]
    data = analytics.series(filled, "tn", "77", [indicator], payer="total")
    points = data["series"][str(indicator)]["points"]
    assert len(points) == 3
    assert all(point["avg"] > 0 for point in points)
    assert all(1 <= point["rank"] <= point["subjects"] for point in points)


def test_ranking_is_sorted_desc(filled):
    indicator = analytics.indicators(filled, "tn", 2020)[0]["id"]
    rows = analytics.ranking(filled, "tn", 2020, indicator, "total", limit=10)
    values = [row["value"] for row in rows]
    assert values == sorted(values, reverse=True)


def test_benefits_are_split_by_payer(filled):
    fl = analytics.benefits(filled, "tn", 2020, "77", payer="fl")
    ul = analytics.benefits(filled, "tn", 2020, "77", payer="ul")
    assert all(item["payer"] == "fl" for item in fl["items"])
    assert all(item["payer"] == "ul" for item in ul["items"])
    assert fl["by_payer"] == ul["by_payer"]


def test_wordcloud_excludes_boilerplate_and_splits_categories(filled):
    cloud = analytics.wordcloud(filled, "tn", "fl", years_selected=[2020], limit=50)
    words = {item["text"] for item in cloud["items"]}
    assert words, "облако не должно быть пустым"
    assert not ({"статьи", "налога", "уплаты", "закона"} & words)
    ul_cloud = analytics.wordcloud(filled, "tn", "ul", years_selected=[2020], limit=50)
    ul_words = {item["text"] for item in ul_cloud["items"]}
    assert "организации" in ul_words
    assert "организации" not in words


def test_wordcloud_distinctive_mode_reorders(filled):
    freq = analytics.wordcloud(filled, "tn", "ip", years_selected=[2020], limit=20)
    distinctive = analytics.wordcloud(filled, "tn", "ip", years_selected=[2020],
                                      limit=20, mode="distinctive")
    assert [i["text"] for i in freq["items"]] != [i["text"] for i in distinctive["items"]]


def test_api_endpoints(filled, tmp_path):
    client = TestClient(create_app(filled.execute("PRAGMA database_list").fetchone()[2]))
    assert client.get("/api/meta").json()["counts"]["benefits"] > 0
    assert client.get("/api/regions", params={"tax": "tn", "year": 2020}).status_code == 200
    profile = client.get("/api/profile", params={"tax": "tn", "year": 2020, "region": "50"})
    assert profile.json()["region"]["code"] == "50"
    form = client.get("/api/form", params={"tax": "tn", "year": 2020, "region": "50"}).json()
    assert form and "total" in form[0]
    cloud = client.get("/api/wordcloud", params={"tax": "tn", "payer": "fl", "years": 2020})
    assert cloud.json()["items"]
    csv_export = client.get("/api/export/benefits.csv",
                            params={"tax": "tn", "year": 2020, "region": "50"})
    assert csv_export.status_code == 200
    assert "category" in csv_export.text.splitlines()[0]
