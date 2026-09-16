# OSCARS XAS Demonstrator

An end-to-end demonstrator for **findable, interoperable X-ray absorption
spectroscopy (XAS)** built for the OSCARS project
([Findable big data for various material characterisation techniques](https://oscars-project.eu/projects/findable-big-data-various-material-characterisation-techniques-0)).

It shows a single FAIR pipeline that:

1. **finds** public XAS data at **two facilities** through their own discovery mechanisms:
   - **ESRF (beamline ID21)** via its **ICAT+** catalogue, wrapped by the
     [`nomad-semantic-web-service`](https://github.com/FAIRmat-NFDI/nomad-semantic-web-service) package: a PaNET→ESRFET **ontology mapping** resolves the technique term, which then filters the catalogue server-side, and the data is downloaded anonymously;
   - **BESSY II** via the **NOMAD search API**, querying by the pynxtools NeXus **application definition** (`definition == NXxas`) rather than by location;
2. **standardizes** it to the NeXus **`NXxas`** application definition with [`pynxtools-xas`](https://github.com/FAIRmat-NFDI/pynxtools-xas) (the ESRF raw `.h5`; BESSY data already arrives as NeXus);
3. **processes** every spectrum through the **ewoks/est** EXAFS workflow
   (`example_pymca.ows`: normalisation → EXAFS → k-weight → Fourier transform);
4. **writes the results back** as NOMAD entries.

The whole thing runs from two Jupyter notebooks in **NORTH** (NOMAD's remote
tools hub) on a NOMAD Oasis (`oasis-b`), so discovery, conversion, processing,
and archival all happen next to the data.

## What's in this repository

| Path | What it is |
|---|---|
| [`oscars-demonstrator/`](oscars-demonstrator/) | **The NOMAD upload bundle** — the two notebooks, their orchestration code (`oscars_demo.py`, `run_pymca_demo.py`), `README.md`, and `nomad.json`. This is the folder you zip and upload to NOMAD. |
| `pyproject.toml` + `uv.lock` | Locked Python environment for running the notebooks locally (outside the upload bundle). |

The demonstrator depends on two FAIRmat packages:
- **`pynxtools-xas`** (the XAS→NXxas reader, including the ESRF ID21 fluorescence parser), and
- **`nomad-semantic-web-service`** (the ESRF semantic discovery + download plugin).

See [`oscars-demonstrator/README.md`](oscars-demonstrator/README.md) for the detailed pipeline and the difference between the two notebooks.

## Set up the local environment

The notebooks run against a uv environment at `./.venv` — **not** checked into git (a fresh clone has no `.venv`; build it from the lock, see [below](#building-the-environment-from-the-lock)). Once built it provides:

- the ewoks/est stack (`est`, `ewoks`, `ewoksorange`, `PyMca5`, `PyQt5`, `Orange3`)
- `pynxtools==0.15.2` + `pynxtools-xas` (editable) + `pynxtools-xps` — so the `pynx` CLI and the XAS reader (incl. the ESRF ID21 parser) are available
- the `nomad-semantic-web-service` catalogue layer (editable) — ESRF discovery
- `jupyterlab` + `jupyterlab-h5web`

and registers a Jupyter kernel **“OSCARS XAS (est+pynxtools)”** (`oscars-xas`).

## Run the notebooks locally

```console
$ cd oscars-demonstrator
$ QT_QPA_PLATFORM=offscreen PATH="../.venv/bin:$PATH" ../.venv/bin/jupyter lab
```

Open `1_full_pipeline.ipynb`, pick the **“OSCARS XAS (est+pynxtools)”** kernel, and run all cells. Downloaded raw files land in `downloads/` and processed results in `results/`, both under `oscars-demonstrator/`. `QT_QPA_PLATFORM=offscreen` is needed because the ewoks graph pulls in Orange's GUI import; the notebook's setup cell also sets it.

- `1_full_pipeline.ipynb` — pulls ESRF ID21 (semantic map + ICAT+ + IDS download + `pynxtools-xas` convert) and BESSY (NOMAD search) from the notebook.
- `2_eln_esrf_plus_bessy.ipynb` — expects the ESRF data to have been pulled + auto-converted by the plugin's ELN inside NOMAD; run it there, or locally it will just process BESSY.

## Run it on NOMAD (Oasis + NORTH)

The demonstrator is designed to run **inside NOMAD**, where the notebooks, the downloaded data, and the results all live in one upload.

1. **Deploy the plugins.** The target Oasis (here `oasis-b`) must have the demonstrator's plugins installed — in `nomad-distro`'s `pyproject.toml`: `pynxtools==0.15.2` (ships the NXxas definitions), `pynxtools-xas==0.1.1` (the ID21 fluorescence parser), and `nomad-semantic-web-service==0.1.0` (ESRF discovery + ELN). NORTH Jupyter (`nomad-north-jupyter`) is already in the distro.
2. **Create the upload.** In the NOMAD GUI, create a new upload and add the contents of [`oscars-demonstrator/`](oscars-demonstrator/) (zip the folder and drop it in, or upload the files). The bundled `nomad.json` supplies the upload's comment/references; any `.nxs`/`.h5` files are parsed by `pynxtools-xas` into their own entries.
3. **Launch NORTH.** From the upload, start **Jupyter** via NORTH. NORTH mounts the upload's raw directory into the container, so anything the notebooks write into `downloads/`/`results/` becomes a raw file in the upload and is processed into entries — no separate upload step. The container has egress to ESRF ICAT+ (`icatplus.esrf.fr`) and the NOMAD search API.
4. **Run a notebook.** Open `1_full_pipeline.ipynb` and run it, or use `2_eln_esrf_plus_bessy.ipynb` together with the plugin's **Dataset search request** ELN (its defaults already target ESRF / XAS / ID21 / 2021–2022) to pull and auto-convert the ESRF data inside NOMAD first.

## Building the environment from the lock

The PyPI dependencies are pinned in `pyproject.toml` + `uv.lock` here at the repo root. `uv sync` recreates `./.venv` from them; the two local packages are then layered in editable + `--no-deps` (this keeps `pynxtools` pinned to 0.15.2 and avoids pulling `nomad-lab` — only the standalone catalogue layer is used), and the Jupyter kernel is registered.

```console
$ cd <path-to-oscars-demonstrator-repo>/
$ uv sync                       # builds ./.venv from pyproject.toml + uv.lock
$ uv pip install --no-deps -e ../nomad-distro-dev/packages/pynxtools-xas
$ uv pip install --no-deps -e ../nomad-distro-dev/packages/nomad-semantic-web-service
$ .venv/bin/python -m ipykernel install --user --name oscars-xas \
    --display-name "OSCARS XAS (est+pynxtools)"
```

Most configuration (BESSY endpoint, upload id, per-facility processing cap, ...)
is plain constants near the top of each notebook — edit them inline rather than
through the environment. The one real environment variable is:

* `NOMAD_SWS_SRC` — dev path to the `nomad-semantic-web-service` `src` (only if
  the package isn't installed)