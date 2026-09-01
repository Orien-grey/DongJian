from chongzu.extract.table_regions import detect_table_regions


def test_outer_blank_margin_and_two_regions() -> None:
    rows = [
        [None, None, None],
        [None, "a", "b"],
        [None, 1, 2],
        [None, None, None],
        [None, "c", "d"],
        [None, 3, 4],
    ]
    regions = detect_table_regions(rows)
    assert [(item.row_start, item.row_end, item.column_start, item.column_end) for item in regions] == [
        (1, 3, 1, 3),
        (4, 6, 1, 3),
    ]
    assert all("multiple_table_regions" in item.warnings for item in regions)


def test_adjacent_title_is_not_cut_and_is_flagged() -> None:
    regions = detect_table_regions([["Annual report", None], ["area", "value"], ["A", 1]])
    assert len(regions) == 1
    assert "possible_title_row" in regions[0].warnings
    assert "ambiguous_table_boundary" in regions[0].warnings
