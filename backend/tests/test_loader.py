import xlsxwriter

from app import loader


def _write_workbook(path, sheets):
    workbook = xlsxwriter.Workbook(path)
    try:
        for sheet in sheets:
            sheet_name, columns, rows = sheet[:3]
            start_row = sheet[3] if len(sheet) > 3 else 0
            preamble = sheet[4] if len(sheet) > 4 else []
            worksheet = workbook.add_worksheet(sheet_name)
            for row_index, row in preamble:
                worksheet.write_row(row_index, 0, row)
            worksheet.write_row(start_row, 0, columns)
            for row_index, row in enumerate(rows, start=start_row + 1):
                worksheet.write_row(row_index, 0, row)
    finally:
        workbook.close()


def test_excel_same_column_sheets_are_consolidated(tmp_path):
    path = tmp_path / "split.xlsx"
    _write_workbook(
        path,
        [
            ("part1", ["id", "amount"], [(1, 10), (2, 20), (3, 30)]),
            ("part2", ["id", "amount"], [(4, 40), (5, 50)]),
        ],
    )

    frame = loader.read_table(path)

    assert frame.shape == (5, 2)
    assert frame.rows() == [(1, 10), (2, 20), (3, 30), (4, 40), (5, 50)]


def test_excel_info_header_row_is_autodetected(tmp_path):
    path = tmp_path / "offset.xlsx"
    _write_workbook(
        path,
        [
            (
                "export",
                ["id", "amount"],
                [(1, 10), (2, 20)],
                4,
                [
                    (0, ["Accounts payable extract"]),
                    (1, ["Generated", "2026-07-07"]),
                ],
            ),
        ],
    )

    frame = loader.read_table(path)

    assert frame.columns == ["id", "amount"]
    assert frame.rows() == [(1, 10), (2, 20)]


def test_excel_same_column_sheets_with_info_headers_are_consolidated(tmp_path):
    path = tmp_path / "split_offset.xlsx"
    preamble = [
        (0, ["Accounts payable extract"]),
        (1, ["Generated", "2026-07-07"]),
    ]
    _write_workbook(
        path,
        [
            ("part1", ["id", "amount"], [(1, 10), (2, 20), (3, 30)], 4, preamble),
            ("part2", ["id", "amount"], [(4, 40), (5, 50)], 4, preamble),
        ],
    )

    frame = loader.read_table(path)

    assert frame.shape == (5, 2)
    assert frame.rows() == [(1, 10), (2, 20), (3, 30), (4, 40), (5, 50)]


def test_excel_multi_sheet_workbook_with_same_columns_is_consolidated(tmp_path):
    path = tmp_path / "normal.xlsx"
    _write_workbook(
        path,
        [
            ("current", ["id", "amount"], [(1, 10), (2, 20)]),
            ("prior", ["id", "amount"], [(3, 30), (4, 40)]),
        ],
    )

    frame = loader.read_table(path)

    assert frame.shape == (4, 2)
    assert frame.rows() == [(1, 10), (2, 20), (3, 30), (4, 40)]


def test_excel_multi_sheet_workbook_with_different_columns_keeps_first_sheet(tmp_path):
    path = tmp_path / "different.xlsx"
    _write_workbook(
        path,
        [
            ("transactions", ["id", "amount"], [(1, 10), (2, 20)]),
            ("customers", ["id", "name"], [(1, "Alpha"), (2, "Beta")]),
        ],
    )

    frame = loader.read_table(path)

    assert frame.shape == (2, 2)
    assert frame.columns == ["id", "amount"]
    assert frame.rows() == [(1, 10), (2, 20)]
