# Generator

The pack is reproducible. Run in order from this directory:

```bash
python gen_data.py && python verify.py && python gen_docs.py && python gen_guide.py
```

- `gen_data.py` builds the ten CSVs in `../data/` and writes `ground_truth.json`.
  The baseline is generated under live dealer, counterparty and rate
  constraints, so every exception in the files is one the script injected on
  purpose. It is seeded, so the output is deterministic.
- `verify.py` re-derives every exception from the written CSVs without looking
  at the generator's state, and reconciles what it finds to `ground_truth.json`.
  It prints `all reconciled` when the populations carry exactly the seeded
  exceptions and nothing else. Run it after any change.
- `gen_docs.py` builds the three criteria documents and the 18 deal packs from
  `ground_truth.json`, so the paper and the populations agree except where an
  exception makes them disagree. Each pack is a folder and each document inside
  it is its own single-page PDF, named for its own reference and its type,
  because intake treats one file as one document and classifies on the
  filename.
- `gen_guide.py` writes `../FACILITATOR_GUIDE.md` from `ground_truth.json`, so
  the answer key cannot drift from the files.

`gen_data.py` and `verify.py` need `polars`; `gen_docs.py` needs `reportlab` and
`python-docx`.

To change the exception set, edit the injection blocks in `gen_data.py` and run
the four scripts again. If `verify.py` reports an extra, the baseline grew an
exception nobody seeded and the guide would be wrong.
