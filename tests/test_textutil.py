from fnsportal.textutil import (clean, detect_payer_single, norm_key, parse_number,
                                parse_year, stem, stopword_set, terms)


def test_stem_groups_word_forms():
    assert stem("многодетные") == stem("многодетным") == stem("многодетных")
    assert stem("организациями") == stem("организации") == stem("организация")
    assert stem("инвалидов") == stem("инвалиды") == "инвалид"


def test_detect_payer_by_head_noun():
    assert detect_payer_single("Физические лица") == "fl"
    assert detect_payer_single("Организации инвалидов") == "ul"
    assert detect_payer_single("Инвалиды I и II групп") == "fl"
    assert detect_payer_single("Индивидуальные предприниматели") == "ip"
    # ИП внутри длинной формулировки всё равно выигрывает
    assert detect_payer_single(
        "Физические лица, зарегистрированные в качестве индивидуальных предпринимателей") == "ip"
    assert detect_payer_single("Все категории налогоплательщиков") == "all"


def test_parse_numbers_and_years():
    assert parse_number("1 234,56") == 1234.56
    assert parse_number("2,2 %") == 2.2
    assert parse_number("-") is None
    assert parse_year("за 2019 год") == 2019
    assert parse_year("нет данных") is None


def test_norm_key_and_clean():
    assert norm_key("Код  региона (Б)") == "код региона б"
    assert clean("а б\n в") == "а б в"


def test_terms_drop_legal_boilerplate():
    stops = stopword_set()
    text = ("Многодетные семьи, имеющие трёх детей, освобождаются от уплаты "
            "налога в соответствии с пунктом 2 статьи 4 Закона области")
    found = {surface for _, surface, is_bigram in terms(text, stops) if not is_bigram}
    assert "многодетные" in found and "семьи" in found
    for boilerplate in ("статьи", "пунктом", "закона", "налога", "уплаты"):
        assert boilerplate not in found
