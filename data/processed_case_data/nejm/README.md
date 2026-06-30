## SDBench "nejm" dataset

The `nejm` dataset is sourced from recent cases of the New England Journal of Medicine (NEJM) Clinical Pathological Conference (CPC), published as Case Records of the Massachusetts General Hospital. We are unable to directly distribute this dataset.

To run SDBench on the `nejm` dataset you will need to license the SDBench dataset from NEJM.

The recommended approach is to email:

```
To: elicensing@nejm.org
Subject: SDBench dataset access
----
I would like to license the SDBench dataset of NEJM CPC cases
My NEJM subscription email address is xxx@yyy.com
```

NEJM will advise you about licensing, delivery, and any applicable terms. Once you receive the `dataset.json` file, put it in this directory.

Run this CLI utility to confirm that the dataset is ready to use:

```
uv run python -m sdbench.cli.case_data nejm
```

You can then run SDBench on the `nejm` dataset.
