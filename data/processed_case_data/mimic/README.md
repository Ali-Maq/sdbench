## SDBench "mimic" dataset

The `mimic` dataset is sourced from the [MIMIC-IV](https://physionet.org/content/mimiciv/3.1/) dataset hosted on [PhysioNet](https://physionet.org). Access to MIMIC-IV data requires a credentialing process and acceptance of a Data Use Agreement so we are unable to directly distribute this dataset, but we are re-distributing it via PhysioNet as `MIMIC-IV-Ext-SDBench` under the same conditions as the original MIMIC-IV data it is based on.

To get the dataset first check you can access [MIMIC-IV-Ext-SDBench](https://physionet.org/content/mimic-iv-ext-sdbench/1.0.0/) and see if the files are available at the bottom of that page. If not you will need to potentially create a credentialed user account, complete the required training and accept the data use agreement.

Once you can see the files you can either download the `dataset.json` file into this directory or run the following from the repo root with your correct `PHYSIONET_USERNAME`:

```
wget --user PHYSIONET_USERNAME --ask-password -O data/processed_case_data/mimic/dataset.json https://physionet.org/files/mimic-iv-ext-sdbench/1.0.0/dataset.json
```

Run this CLI utility to confirm that the dataset is ready to use

```
uv run python -m sdbench.cli.case_data mimic
```

then you can run SDBench on the `mimic` dataset.
