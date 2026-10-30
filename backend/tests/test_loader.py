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


def test_file_signature_is_the_content_not_where_or_when_it_was_written(tmp_path):
    """Moving or unzipping an engagement must not make its data look changed.

    Every persisted judgement about a table is keyed by this signature. Built on
    the resolved path and mtime, a workspace moved under a new home reopened
    every table pair the join stage had already settled.
    """
    import os
    import shutil

    original = tmp_path / "a" / "invoices.csv"
    original.parent.mkdir()
    original.write_text("id,amount\n1,10\n2,20\n", encoding="utf-8")
    moved = tmp_path / "b" / "invoices.csv"
    moved.parent.mkdir()
    shutil.copyfile(original, moved)
    stat = moved.stat()
    os.utime(moved, ns=(stat.st_atime_ns, stat.st_mtime_ns + 7_000_000_000))

    assert loader.file_signature(moved) == loader.file_signature(original)

    moved.write_text("id,amount\n1,10\n2,21\n", encoding="utf-8")
    assert loader.file_signature(moved) != loader.file_signature(original)
