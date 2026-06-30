## Grounding Costing Data

Costing data used in the study were obtained from the Kaiser Permanente
Washington Central Hospital Standard Charges file (chargemaster), which is publicly available at https://healthy.kaiserpermanente.org/content/dam/kporg/final/documents/health-plan-documents/coverage-information/910511770-central-hospital-standard-charges-wa-en.xlsx. This file is published by Kaiser Permanente in compliance with the Centers for Medicare & Medicaid Services (CMS) Hospital Price Transparency rule (45 CFR §180), which requires hospitals to make standard charge information publicly available in a machine-readable format free of charge and without restrictions on reuse.

To use this same chargemaster for grounding you will need to download it to `data/costing_data` either manually or with this command:

```bash
wget -q -O data/costing_data/910511770-central-hospital-standard-charges-wa-en.xlsx https://healthy.kaiserpermanente.org/content/dam/kporg/final/documents/health-plan-documents/coverage-information/910511770-central-hospital-standard-charges-wa-en.xlsx
```

The cost grounding code was developed for this chargemaster file but should work with any other file in the exact same format.
