# OSCARS XAS Demonstrator

OSCARS project:
<https://oscars-project.eu/projects/findable-big-data-various-material-characterisation-techniques-0>

End-to-end demonstrator: find public **XAS** data at **two** synchrotron facilities (ESRF, BESSY II) through their own discovery mechanisms, get it into the **NXxas** NeXus application definition, run the **ewoks/est EXAFS** workflow, and write the results back as NOMAD entries — orchestrated from Jupyter notebooks running in **NORTH** on the `oasis-b` NOMAD deployment.

This **NOMAD upload** provides the notebooks (that can be opened with Jupyter via NORTH) and the orchestration code to semantically search, download, convert, and postprocess XAS data from the two facilities. Downloaded raw files and processed results are all parsed into their own entries.

---

## The two data paths

### ESRF (ID21) — via `nomad-semantic-web-service`

ESRF has no NOMAD; discovery goes through its **ICAT+** catalogue, wrapped by the
[`nomad-semantic-web-service`](https://github.com/FAIRmat-NFDI/nomad-semantic-web-service)
plugin (ontology-aware search + anonymous download).

```
PaNET term ──map──▶ ESRFET term          (ontology mapping, owl:equivalentClass over ESRFET.owl)
      │
      ▼
ICAT+ /catalogue/datasets   (instrument=ID21, techniquePids=ESRFET:XAS, date)   ← technique filtered SERVER-SIDE
      │
      ▼
IDS /ids/data/download (anonymous)  ──▶  raw ID21 .h5   (single-file dataset)
      │
      ▼
pynxtools-xas  (EsrfFluoParser, --nxdl NXxas)  ──▶  NXxas .nxs
      │
      ▼
ewoks/est EXAFS workflow  ──▶  results  ──▶  NOMAD entry
```

**Why ID21.** ID21's public datasets *are* annotated with the XAS technique PID, so the resolved ESRFET XAS IRI filters them **server-side**, and they are kept on disk. They are fluorescence-yield µXANES scans; `pynxtools-xas`' `EsrfFluoParser` infers the element and absorption edge from the active
emission-line channel and writes a self-contained NXxas file.

### BESSY II — via the NOMAD search API

BESSY data already lives in a NOMAD oasis as **NXxas `.nxs` entries**. We search *as if we didn't know where it was*, by the pynxtools NeXus **definition**: 
```
NOMAD /entries/query  with search_quantities filter:
    id        = data.ENTRY.definition__field#pynxtools.nomad.schema.Root
    str_value = NXxas
      │
      ▼
/entries/{entry_id}/raw  ──▶  .nxs   (ALREADY NXxas — no conversion)
      │
      ▼
ewoks/est EXAFS workflow  ──▶  results  ──▶  NOMAD entry
```

### Common tail

Both paths converge on a NXxas `.nxs` and run the **same** ewoks/est EXAFS graph (`example_pymca.ows`: Input → Normalization → EXAFS → k-weight → Fourier transform → Output), driven headlessly by `run_workflow.py`. Base-NXxas files (BESSY foils and ESRF ID21 fluorescence alike) carry the signal at
`entry/intensity`, so the workflow is run with `signal="intensity"`. Each result is written back as a NOMAD `.archive.yaml` entry plus a result `.h5`.

---

## The two notebooks

| Notebook | ESRF ID21 comes from | BESSY comes from |
|---|---|---|
| **`1_full_pipeline.ipynb`** | this notebook (catalogue map + ICAT+ + IDS download + `pynxtools-xas` convert) | NOMAD search |
| **`2_eln_esrf_plus_bessy.ipynb`** | the plugin's **ELN**, pulled + auto-converted *inside NOMAD* before you open the notebook | NOMAD search |

Notebook 2's ESRF path is the NOMAD-native variant: create a **Dataset search request** ELN entry (its defaults already target ESRF / XAS / ID21 / 2021–2022 / real ICAT+), tick **Download Files** on a match, and — with auto-convert on (the default) — the `.h5` is downloaded and converted to an `NXxas` `.nxs` entry inside the upload. The notebook then just picks those `.nxs` up.

---

## Running it in NORTH (`oasis-b`)

NORTH mounts the upload's raw directory into the Jupyter container, so anything the notebooks write into `downloads/`/`results/` becomes a raw file in the upload and is processed into entries — no separate upload step. The container has egress to ESRF ICAT+ (`icatplus.esrf.fr`) and the NOMAD search API. The ewoks stack is `pip install`ed into the kernel by the setup cell if the image doesn't already
carry it (`QT_QPA_PLATFORM=offscreen` is set for the headless Orange import).

---

## Files

| File | Role |
|---|---|
| `1_full_pipeline.ipynb` | notebook 1 — pull everything from the notebook |
| `2_eln_esrf_plus_bessy.ipynb` | notebook 2 — ESRF via the ELN + BESSY |
| `oscars_demo.py` | orchestration helpers (map/search/download/convert/ewoks/result) |
| `run_workflow.py` | headless ewoks/est EXAFS runner (NXxas-aware) |
| `nomad.json` | upload metadata (comment + references) |