"""OSCARS XAS demonstrator — orchestration helpers.

Reusable, mostly-pure functions behind the OSCARS XAS demonstrator notebooks.
Kept in a module (not inline in the notebook) so the non-ewoks parts
can be unit/smoke-tested against the real services.

Two discovery paths, one common tail:

* ``esrf_*``   — ESRF discovery via the **nomad-semantic-web-service** package:
  PaNET→ESRFET ontology mapping + ICAT+ catalogue + anonymous IDS download
  (Path A). These functions delegate to``nomad_semantic_web_service.catalogue``
  rather than re-implementing ICAT; the semantic mapping (``esrf_map_technique``)
  is one of the core OSCARS deliverables.
* ``bessy_*``  — NOMAD search-by-NXxas-definition + raw download (Path B)
* ``convert_to_nxxas`` — pynxtools-xas ``.h5`` -> NXxas ``.nxs`` (Path A only;
  BESSY files are already NXxas)
* ``run_ewoks_exafs`` — the ewoks/est EXAFS workflow on a ``.nxs``
* ``write_result_entry`` — drop a NOMAD ``.archive.yaml`` next to the outputs

See README.md and oscars-demonstrator/README.md for the full picture.

The ESRF side needs the ``nomad-semantic-web-service`` package importable.
In the dev tree, point ``NOMAD_SWS_SRC`` at a local clone of ``nomad-semantic-web-service/src``
(this module adds it to ``sys.path`` automatically if set).
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Literal

import requests

# --------------------------------------------------------------------------
# nomad-semantic-web-service catalogue layer (the ESRF deliverable)
# --------------------------------------------------------------------------
# Its domain layer (catalogue.icat / catalogue.ontology / catalogue.search) has
# no FastAPI/nomad dependency, so it imports standalone here. See the plugin's
# docs/explanation.md ("schemas != runtime logic").

_sws_src = os.environ.get("NOMAD_SWS_SRC")
if _sws_src and _sws_src not in sys.path:
    sys.path.insert(0, _sws_src)


def _require(module: Literal["icat", "search"]):
    """Import one ``nomad_semantic_web_service.catalogue`` submodule.

    Raises a clear error if the package isn't importable. (``ontology`` is never
    imported here directly — ``search.resolve_technique_term`` pulls it in
    lazily only when a PaNET term actually needs mapping, keeping owlready2 off
    the import path otherwise.)
    """
    import importlib

    try:
        return importlib.import_module(f"nomad_semantic_web_service.catalogue.{module}")
    except ModuleNotFoundError as exc:  # pragma: no cover - env guard
        raise ModuleNotFoundError(
            "nomad-semantic-web-service is required for the ESRF path. "
            "Install it (`pip install nomad-semantic-web-service`) or set "
            "NOMAD_SWS_SRC to its 'src' directory."
        ) from exc


# --------------------------------------------------------------------------
# NOMAD deployments. The BESSY XAS demo data currently lives on the FAIRmat
# staging server (upload zMg1PGypQRa4yA05cyM8Pw); the live BESSY oasis is
# nomad-bessy2.helmholtz-berlin.de. Both speak the same API.
NOMAD_STAGING = "https://nomad-lab.eu/prod/v1/staging/api/v1"
NOMAD_BESSY = "https://nomad-bessy2.helmholtz-berlin.de/nomad-oasis/api/v1"
NOMAD_OASIS_B = "https://nomad-lab.eu/prod/v1/oasis-b/api/v1"

# The pynxtools search quantity that carries the NeXus application definition.
NXXAS_DEFINITION_QUANTITY = "data.ENTRY.definition__field#pynxtools.nomad.schema.Root"


# ==========================================================================
# NXxas discovery — search ONE NOMAD deployment for NXxas entries + download
# ==========================================================================
# Facility-agnostic: the *same* two functions discover and fetch NXxas entries
# from whichever deployment is passed as `nomad_api`. The demonstrator calls
# them once per deployment — the BESSY oasis for BESSY foils, and oasis-b for
# ESRF data that was converted to NXxas there. Entries for the two facilities
# live on different deployments, so there is no single cross-facility index;
# the unification is this one query shape run against each endpoint.


def search_nxxas_entries(
    nomad_api: str = NOMAD_STAGING,
    definition: str = "NXxas",
    upload_id: str | None = None,
    owner: str = "visible",
    page_size: int = 50,
    token: str | None = None,
) -> list[dict[str, Any]]:
    """Search ONE NOMAD deployment for entries whose pynxtools NeXus definition
    == *definition* (e.g. ``NXxas``).

    *nomad_api* selects the deployment (e.g. the BESSY oasis, or oasis-b). Each
    returned ``{entry_id, upload_id, mainfile}`` dict is tagged with
    ``nomad_api`` so a later ``download_nxs_entry`` knows which deployment to
    pull from.
    """
    query: dict[str, Any] = {
        "search_quantities": {
            "id": NXXAS_DEFINITION_QUANTITY,
            "str_value": definition,
        }
    }
    if upload_id:
        query["upload_id"] = upload_id

    headers = {"content-type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.post(
        f"{nomad_api}/entries/query",
        headers=headers,
        json={
            "owner": owner,
            "query": query,
            "pagination": {"page_size": page_size},
            "required": {"include": ["entry_id", "upload_id", "mainfile"]},
        },
        timeout=30,
    )
    resp.raise_for_status()
    hits = resp.json().get("data", [])
    for hit in hits:
        hit["nomad_api"] = nomad_api  # remember which deployment this came from
    return hits


def download_nxs_entry(
    entry_id: str,
    dest_dir: Path,
    nomad_api: str = NOMAD_STAGING,
    token: str | None = None,
) -> list[Path]:
    """Download one entry's raw files (zip) from *nomad_api* and extract the
    ``.nxs`` file(s).

    Equivalent to ``nomad_utility_workflows...download_entry_raw_data_by_id``,
    inlined so the notebook has no hard dependency on that package. Facility-
    agnostic — pass the ``nomad_api`` the entry was found on (e.g. from a
    ``search_nxxas_entries`` hit's ``nomad_api`` field).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    headers = {"accept": "application/zip"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(
        f"{nomad_api}/entries/{entry_id}/raw",
        headers=headers,
        params={"compress": "true"},
        timeout=120,
    )
    resp.raise_for_status()

    extracted: list[Path] = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.endswith(".nxs"):
                continue
            out = dest_dir / Path(info.filename).name
            out.write_bytes(zf.read(info))
            extracted.append(out)
    return extracted


# ==========================================================================
# Path A — ESRF: thin wrapper around the nomad-semantic-web-service package
# ==========================================================================
# The ESRF domain logic lives in `nomad_semantic_web_service.catalogue`, which
# is the OSCARS ESRF deliverable (semantic PaNET/ESRFET mapping + ICAT+
# catalogue + anonymous IDS download). We do NOT re-implement any of it here:
#
#   * technique resolution  -> catalogue.search.resolve_technique_term
#   * dataset listing        -> catalogue.icat.fetch_icat_catalogue_datasets
#   * online/archive status  -> catalogue.icat.get_datasets_status
#   * download + extract     -> catalogue.icat.download_and_extract
#
# The only thing left in this module is genuine notebook orchestration that
# isn't ESRF domain logic: a tape/local-cache resilience fallback for the
# live demo (esrf_download_h5). Everything else is re-exported so the notebook
# keeps a single import surface without a private ICAT copy.


def esrf_map_technique(term: str, vocabulary: str | None = None) -> dict[str, Any]:
    """Resolve a technique term to an ESRFET IRI (the semantic step).

    Thin pass-through to ``catalogue.search.resolve_technique_term`` — the one
    shared implementation used by the ELN and REST API too. Returns its dict:
    ``{"input", "vocabulary", "resolved_iri", "relation", "mappings", "warning"}``.
    """
    return _require("search").resolve_technique_term(term, vocabulary)


def esrf_search_datasets(
    start_date: date,
    end_date: date,
    beamline: str = "ID21",
    technique_pids: str | None = "https://w3id.org/PaN/ESRFET#XAS",
    limit: int = 25,
) -> list[dict[str, Any]]:
    """List ESRF datasets for a beamline+date window (raw ICAT+ records).

    Straight pass-through to ``catalogue.icat.fetch_icat_catalogue_datasets``;
    returns the ICAT+ record dicts unchanged (``d["id"]``, ``d["sampleName"]``,
    ``d["investigation"]``, …).

    Defaults target ID21, whose public datasets *are* annotated with the XAS
    technique (so ``technique_pids`` filters them server-side) and are kept on
    disk. This is the semantic step in action: the ESRFET XAS IRI here is what
    ``esrf_map_technique`` resolves a PaNET/ESRFET term to. ESRF's tape-archived
    EXAFS beamlines (e.g. BM23) are unannotated and not reliably restorable on
    demand — see ESRF_ICAT.md.
    """
    return _require("icat").fetch_icat_catalogue_datasets(
        start_date=start_date,
        end_date=end_date,
        instrument_name=beamline,
        technique_pids=technique_pids,
        limit=limit,
    )


def esrf_dataset_status(dataset_ids: list[int]) -> dict[int, str]:
    """IDS online/archive status — pass-through to catalogue.icat."""
    return _require("icat").get_datasets_status(dataset_ids)


def esrf_request_restore(dataset_id: int) -> None:
    """Queue a tape restore (fire-and-forget) via catalogue.icat helpers."""
    import httpx

    icat = _require("icat")
    with httpx.Client(timeout=30) as client:
        sid = icat.get_anonymous_session_id(dataset_id, client=client)
        icat.request_dataset_restore(dataset_id, sid, client=client)


def esrf_download_h5(
    dataset_id: int,
    dest_dir: Path,
    local_cache_dir: Path | None = None,
    sample_name: str | None = None,
) -> list[Path]:
    """Download a dataset's ``.h5`` files, with a demo tape/local-cache fallback.

    The download+extract itself is ``catalogue.icat.download_and_extract``
    (anonymous, `.h5`-filtered, raises ``DatasetNotOnlineError`` for tape-
    archived data). The *only* added behavior — and the reason this stays in the
    notebook layer rather than the package — is demo resilience: if the dataset
    is on tape and *local_cache_dir* holds a matching ``.h5``, use the cached
    copy so the live demo still runs (see WORKFLOW_DESIGN.md §5).
    """
    icat = _require("icat")
    try:
        return icat.download_and_extract(
            dataset_id, dest_dir, file_extensions=["h5"]
        )
    except icat.DatasetNotOnlineError:
        cached = _local_cache_lookup(local_cache_dir, sample_name)
        if cached:
            return cached
        raise


def _local_cache_lookup(
    local_cache_dir: Path | None, sample_name: str | None
) -> list[Path]:
    if not local_cache_dir:
        return []
    local_cache_dir = Path(local_cache_dir)
    if not local_cache_dir.is_dir():
        return []
    if sample_name:
        stem = sample_name.split()[0]
        hits = sorted(local_cache_dir.glob(f"{stem}*.h5"))
        if hits:
            return hits
    return sorted(local_cache_dir.glob("*.h5"))


# ==========================================================================
# Convert (Path A only) — pynxtools-xas .h5 -> NXxas .nxs
# ==========================================================================


def convert_to_nxxas(
    h5_file: Path,
    nxs_out: Path,
    nxdl: str = "NXxas",
    pynx: str = "pynx",
) -> Path:
    """Run ``pynx convert --reader xas`` to produce a NXxas ``.nxs``.

    *pynx* is the CLI path (the pynxtools-xas venv's ``.venv/bin/pynx`` in dev,
    or plain ``pynx`` when the environment is on PATH).
    """
    import subprocess

    nxs_out = Path(nxs_out)
    nxs_out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        pynx, "convert", str(h5_file),
        "--reader", "xas",
        "--nxdl", nxdl,
        "--ignore-undocumented",
        "--output", str(nxs_out),
    ]
    subprocess.run(cmd, check=True)
    return nxs_out


def nxxas_has_signal(nxs_file: Path) -> bool:
    """True if *nxs_file* has at least one entry with a usable energy+intensity.

    ``pynx convert`` still exits 0 (with warnings) when no parser matches a file
    and writes a skeleton ``.nxs`` with empty/broken energy+intensity — e.g. an
    ESRF dataset's sparse metadata-only sibling ``.h5``. This checks the produced
    file actually carries a spectrum so such empties can be dropped.
    """
    import h5py

    try:
        with h5py.File(nxs_file, "r") as h5:
            for key in h5:
                entry = h5[key]
                if not isinstance(entry, h5py.Group):
                    continue
                energy, intensity = entry.get("energy"), entry.get("intensity")
                if (
                    energy is not None
                    and intensity is not None
                    and getattr(energy, "ndim", 0) == 1
                    and energy.shape[0] > 1
                    and energy.shape == intensity.shape
                ):
                    return True
    except Exception:
        return False
    return False


def convert_all_to_nxxas(
    h5_files: list[Path],
    dest_dir: Path,
    nxdl: str = "NXxas",
    pynx: str = "pynx",
) -> list[Path]:
    """Convert every ``.h5`` in *h5_files* to NXxas, keeping only valid outputs.

    Converts each file (writing ``<stem>.nxs`` beside it in *dest_dir*) and keeps
    those that produce a real spectrum (see ``nxxas_has_signal``); files no parser
    recognizes are skipped with a message. Returns the produced ``.nxs`` paths.
    """
    dest_dir = Path(dest_dir)
    produced: list[Path] = []
    for h5_file in h5_files:
        h5_file = Path(h5_file)
        nxs_out = dest_dir / (h5_file.stem + ".nxs")
        try:
            convert_to_nxxas(h5_file, nxs_out, nxdl=nxdl, pynx=pynx)
        except Exception as exc:  # pragma: no cover - CLI/env failure
            print(f"   [skip] {h5_file.name}: convert failed ({type(exc).__name__})")
            continue
        if nxxas_has_signal(nxs_out):
            produced.append(nxs_out)
        else:
            nxs_out.unlink(missing_ok=True)
            print(f"   [skip] {h5_file.name}: no XAS spectrum (unrecognized layout)")
    return produced


# ==========================================================================
# Common tail — ewoks EXAFS workflow + result entry
# ==========================================================================

# Non-physical "arbitrary units" spellings. est/PyMca resolves dataset units
# with pint, which parses "au" as *astronomical_unit* (a length) and then fails
# ("Cannot convert from 'astronomical_unit' to 'dimensionless'"). NXxas
# intensity is dimensionless-ish; stripping these lets the workflow run.
_ARBITRARY_UNITS = {"au", "a.u.", "a.u", "arb", "arb.", "arbitrary", "arb. units"}


def sanitize_signal_units(nxs_file: Path) -> Path:
    """Strip non-physical 'arbitrary unit' tags from energy/intensity in place.

    Operates on the given (locally downloaded) ``.nxs`` and returns it. Only
    touches units that pint would misread; real units (eV, keV) are left alone.
    """
    import h5py

    nxs_file = Path(nxs_file)
    with h5py.File(nxs_file, "r+") as h5:
        for ekey in h5:
            entry = h5[ekey]
            if not isinstance(entry, h5py.Group):
                continue
            for rel in ("intensity", "data/intensity", "energy", "data/energy"):
                node = entry.get(rel)
                if node is None or "units" not in node.attrs:
                    continue
                unit = node.attrs["units"]
                unit_str = unit.decode() if isinstance(unit, bytes) else str(unit)
                if unit_str.strip().lower() in _ARBITRARY_UNITS:
                    del node.attrs["units"]
    return nxs_file


def run_ewoks_exafs(
    nxs_file: Path,
    output_h5: Path,
    signal: str = "mu_trans",
    energy_unit: str = "electron_volt",
    run_pymca_demo_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the ewoks/est EXAFS workflow on a NXxas ``.nxs``.

    Thin wrapper over ``run_pymca_demo.py``'s proven headless path (NXxas
    detection + est PyMca graph). That file is kept **beside this module** (a
    sibling in the demonstrator folder), not referenced from the wider repo, so
    the whole demonstrator uploads to NOMAD/NORTH as one self-contained folder.
    Requires the ewoks stack (``ewoks``, ``est``, ``ewoksorange``) in the kernel
    and, headless, ``QT_QPA_PLATFORM=offscreen``.

    For base-NXxas files (BESSY) the processed signal lives at
    ``entry/intensity`` — pass ``signal="intensity"``. For ESRF NXxas_trans,
    ``signal="mu_trans"`` resolves to the itrans detector.
    """
    import importlib.util
    import os
    import sys

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    demo_dir = Path(run_pymca_demo_dir or Path(__file__).resolve().parent)
    demo_py = demo_dir / "run_pymca_demo.py"
    spec = importlib.util.spec_from_file_location("run_pymca_demo", demo_py)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_pymca_demo"] = mod
    spec.loader.exec_module(mod)

    nxs_file = sanitize_signal_units(Path(nxs_file))

    scan = mod.best_compatible_scan(Path(nxs_file), "Emono", signal)
    info = mod.hdf5_input_information(
        Path(nxs_file), scan=scan, energy="Emono", signal=signal,
        energy_unit=energy_unit,
    )
    result = mod.execute_pymca_workflow(
        input_information=info,
        input_file=Path(nxs_file),
        output_file=Path(output_h5),
        scan=scan,
    )
    return {"scan": scan, "result_file": str(output_h5), "raw_result": result}


def write_result_entry(
    archive_yaml: Path,
    *,
    source_facility: str,
    source_id: str,
    nxs_file: str,
    result_file: str,
    element: str | None = None,
    edge: str | None = None,
    sample_name: str | None = None,
) -> Path:
    """Write a minimal NOMAD ``.archive.yaml`` recording one processed result.

    Dropped into the upload's raw dir, NOMAD processes it into its own entry
    (the write-raw-file pattern; see WORKFLOW_DESIGN.md §2). Uses a generic ELN
    schema so it works without a bespoke result schema being deployed.
    """
    import yaml

    archive_yaml = Path(archive_yaml)
    data = {
        "data": {
            "m_def": "nomad.datamodel.metainfo.eln.ELNProcess",
            "name": f"XAS EXAFS result — {source_facility}:{source_id}",
            "source_facility": source_facility,
            "source_id": source_id,
            "nxs_file": nxs_file,
            "result_file": result_file,
            "element": element,
            "edge": edge,
            "sample_name": sample_name,
        }
    }
    data["data"] = {k: v for k, v in data["data"].items() if v is not None}
    archive_yaml.parent.mkdir(parents=True, exist_ok=True)
    with archive_yaml.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    return archive_yaml
