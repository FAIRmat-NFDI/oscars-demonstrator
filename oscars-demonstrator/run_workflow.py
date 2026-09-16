#!/usr/bin/env python3
"""Run the ewoksest PyMca EXAFS tutorial workflow headlessly."""

from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path
from time import perf_counter


ROOT = Path(__file__).resolve().parent
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".cache" / "matplotlib"))
os.environ.setdefault("XDG_CACHE_HOME", str(ROOT / ".cache" / "xdg"))
os.environ.setdefault("PIP_CACHE_DIR", str(ROOT / ".cache" / "pip"))

for cache_dir in (
    Path(os.environ["MPLCONFIGDIR"]),
    Path(os.environ["XDG_CACHE_HOME"]),
    Path(os.environ["PIP_CACHE_DIR"]),
):
    cache_dir.mkdir(parents=True, exist_ok=True)

from ewoks import convert_graph, execute_graph  # noqa: E402
from ewoksorange.gui.workflows.owscheme import ows_to_ewoks  # noqa: E402
from est import resources  # noqa: E402
import h5py  # noqa: E402
import numpy  # noqa: E402


WORKFLOW_STEPS = [
    ("Input", "Read energy and absorption as an XAS object"),
    ("Normalization", "Normalize spectra with the PyMca processor"),
    ("EXAFS", "Extract the EXAFS signal"),
    ("K weight", "Apply the configured k-weighting"),
    ("Fourier transform", "Transform k-space signal to R-space"),
    ("Output", "Write the processed XAS object to HDF5"),
]

ENERGY_PRESETS = {
    "Emono": "instrument/Emono/value",
    "eneenc": "instrument/eneenc/data",
}

SIGNAL_PRESETS = {
    "mu_trans": "instrument/mu_trans/data",
    "mu_ref": "instrument/mu_ref/data",
    "mu_ref2": "instrument/mu_ref2/data",
    "I0": "instrument/I0/data",
    "I1": "instrument/I1/data",
    "I2": "instrument/I2/data",
    "I3": "instrument/I3/data",
}

# NXxas and every NXxas_* subtechnique (herfd, trans, tey, pey, pfy, tfy, ...)
# always store the energy axis at this one fixed field under NXentry: it is
# only ever defined once, in the base NXxas class, never overridden by a
# subtechnique. The subtechnique itself is recorded separately at
# "{scan}/definition" (e.g. "NXxas_trans"). Same preset-dict logic as
# ENERGY_PRESETS above, just resolving to the fixed NXxas field name instead
# of an instrument-specific path.
NXXAS_ENERGY_FIELD = "energy"

NXXAS_ENERGY_PRESETS = {
    "Emono": NXXAS_ENERGY_FIELD,
    "eneenc": NXXAS_ENERGY_FIELD,
}

# Unlike energy, the raw per-channel counters live at a concrete
# NXinstrument/NXdetector path whose name varies by subtechnique (i0,
# itrans, iref, ifluor, iey, ...; see the NXdetector group name= attributes
# in each NXxas_*.nxdl.xml). Rather than hardcoding which subtechnique has
# which detector, this just aliases the generic preset name to the
# candidate detector name(s) and checks the actual file for a match via
# find_detectors(): correct for any subtechnique without listing them, and
# adapts automatically if a converter adds/renames a detector.
NXXAS_SIGNAL_ALIASES: dict[str, tuple[str, ...]] = {
    "I0": ("i0",),
    "I1": ("itrans", "ifluor", "iey"),
    "I2": ("iref",),
    "mu_trans": ("itrans",),
    "mu_ref": ("iref",),
}


def print_banner(title: str) -> None:
    width = 78
    print()
    print("=" * width)
    print(title.center(width))
    print("=" * width, flush=True)


def print_workflow_steps() -> None:
    print_banner("EWOKSEST PYMCA EXAFS WORKFLOW DEMO")
    print("Workflow being executed:")
    for index, (name, detail) in enumerate(WORKFLOW_STEPS, start=1):
        print(f"  [{index:02d}] {name:<18} {detail}")
    print(flush=True)


def find_node_id(workflow: Path, class_name: str) -> str:
    graph = convert_graph(str(workflow), None)
    for node in graph["nodes"]:
        if node["task_identifier"].endswith(class_name):
            return node["id"]
    raise LookupError(f"Could not find a {class_name!r} node in {workflow}")


def dataset_path(scan: str, value: str, presets: dict[str, str]) -> str:
    path = presets.get(value, value).lstrip("/")
    if path.startswith(f"{scan}/"):
        return f"/{path}"
    return f"/{scan}/{path}"


def title_for_scan(h5: h5py.File, scan: str) -> str:
    title = h5.get(f"{scan}/title")
    if title is None:
        return ""
    value = title[()]
    return value.decode() if isinstance(value, bytes) else str(value)


def _nx_class(group: h5py.Group) -> str:
    value = group.attrs.get("NX_class", "")
    return value.decode() if isinstance(value, bytes) else str(value)


def is_nxentry(h5: h5py.File, name: str) -> bool:
    group = h5.get(name)
    return isinstance(group, h5py.Group) and _nx_class(group) == "NXentry"


def entry_definition(h5: h5py.File, scan: str) -> str:
    """Value of "{scan}/definition", e.g. "NXxas" or "NXxas_trans"."""
    node = h5.get(f"{scan}/definition")
    if node is None:
        return ""
    value = node[()]
    return value.decode() if isinstance(value, bytes) else str(value)


def is_nxxas_entry(h5: h5py.File, scan: str) -> bool:
    return entry_definition(h5, scan).startswith("NXxas")


def _attr_names(group: h5py.Group, attr: str) -> list[str]:
    value = group.attrs.get(attr)
    if value is None:
        return []
    if isinstance(value, (bytes, str)):
        value = [value]
    return [item.decode() if isinstance(item, bytes) else str(item) for item in value]


def find_nxdata_groups(h5: h5py.File, scan: str) -> list[str]:
    entry = h5.get(scan)
    if not isinstance(entry, h5py.Group):
        return []
    return [name for name, child in entry.items() if isinstance(child, h5py.Group) and _nx_class(child) == "NXdata"]


def find_detectors(h5: h5py.File, scan: str) -> dict[str, str]:
    """Every NXdetector under "{scan}/instrument", mapped to its data path."""
    instrument = h5.get(f"{scan}/instrument")
    if not isinstance(instrument, h5py.Group):
        return {}
    return {
        name: f"/{scan}/instrument/{name}/data"
        for name, child in instrument.items()
        if isinstance(child, h5py.Group) and _nx_class(child) == "NXdetector" and "data" in child
    }


def nxdata_signals(h5: h5py.File, scan: str) -> dict[str, str]:
    """Every plottable signal for a scan: each NXdata group's ``signal``
    plus its ``auxiliary_signals`` (the NeXus multi-signal convention),
    mapped to their absolute dataset path."""
    signals: dict[str, str] = {}
    for nxdata_name in find_nxdata_groups(h5, scan):
        group = h5[f"{scan}/{nxdata_name}"]
        names = _attr_names(group, "signal") + _attr_names(group, "auxiliary_signals")
        for name in names:
            if name in group:
                signals.setdefault(name, f"/{scan}/{nxdata_name}/{name}")
    return signals


def resolve_energy_path(h5: h5py.File, scan: str, energy: str) -> str:
    """Energy dataset path, using the fixed NXxas layout for NXxas entries."""
    presets = NXXAS_ENERGY_PRESETS if is_nxxas_entry(h5, scan) else ENERGY_PRESETS
    return dataset_path(scan, energy, presets)


def resolve_signal_path(h5: h5py.File, scan: str, signal: str) -> str:
    """Signal dataset path.

    For NXxas entries this resolves a generic preset (I0, mu_trans, ...) to
    whichever candidate NXdetector actually exists in the entry (see
    NXXAS_SIGNAL_ALIASES), and also recognizes any signal declared on the
    entry's NXdata group(s) (its "signal" and "auxiliary_signals"
    attributes), so a technique-specific channel wired in that way can be
    selected by name too.

    If none of that resolves the preset, this does not substitute a
    different signal (e.g. the processed "intensity") -- that would
    misrepresent what was actually measured. Instead it warns and returns a
    literal, most likely nonexistent path so the caller's own
    missing-dataset checks reject it.
    """
    if is_nxxas_entry(h5, scan):
        detectors = find_detectors(h5, scan)
        for candidate in NXXAS_SIGNAL_ALIASES.get(signal, ()):
            if candidate in detectors:
                return detectors[candidate]
        available = nxdata_signals(h5, scan)
        if signal in available:
            return available[signal]
        if signal in SIGNAL_PRESETS:
            print(
                f"Warning: {scan!r} ({entry_definition(h5, scan) or 'NXxas'}) "
                f"does not define a {signal!r} detector or auxiliary signal; "
                "not substituting a different signal.",
                file=sys.stderr,
            )
        return dataset_path(scan, signal, {})
    return dataset_path(scan, signal, SIGNAL_PRESETS)


# Maps a NeXus energy dataset's "units" attribute to the pint unit name est/
# PyMca expects. Anything else is passed through as-is and left to pint, which
# raises a clear error if it can't parse it.
_ENERGY_UNIT_TO_PINT = {
    "ev": "electron_volt",
    "kev": "kiloelectron_volt",
    "electron_volt": "electron_volt",
    "kiloelectron_volt": "kiloelectron_volt",
}

# eV per unit, for the plausibility check in resolve_energy_unit.
_ENERGY_UNIT_TO_EV = {"electron_volt": 1.0, "kiloelectron_volt": 1000.0}


def read_element_edge(h5: h5py.File, scan: str) -> tuple[str | None, str | None]:
    """Element/edge names from "{scan}/element/name" / "{scan}/edge/name", if
    present (the NXxas convention used by both ESRF and BESSY conversions)."""

    def _text(path: str) -> str | None:
        node = h5.get(path)
        if node is None:
            return None
        value = node[()]
        return value.decode() if isinstance(value, bytes) else str(value)

    return _text(f"{scan}/element/name"), _text(f"{scan}/edge/name")


def resolve_energy_unit(h5: h5py.File, scan: str, energy_path: str) -> str:
    """Pint unit name for the dataset at *energy_path*, from its own "units"
    attribute.

    NXxas files self-describe this, and it genuinely varies: ESRF ID21 stores
    eV, BESSY typically keV -- hardcoding one unit for every file/facility
    silently feeds est/PyMca a wrong absolute energy scale for whichever one it
    doesn't match (edge-finding then operates on a physically nonsensical
    range, e.g. 20 eV instead of 20 keV, and normalization "succeeds" anyway
    with degenerate results -- it does not raise).

    If ``xraydb`` and the entry's element/edge are both available, this also
    sanity-checks the resolved unit against the tabulated edge energy and
    *warns* (does not override) when the file's stated unit looks physically
    implausible -- this is a real, observed case: a BESSY myspot NXxas file
    whose energy is actually in keV but tagged "eV".
    """
    raw_unit = h5[energy_path].attrs.get("units", "")
    raw_unit = raw_unit.decode() if isinstance(raw_unit, bytes) else str(raw_unit)
    unit = _ENERGY_UNIT_TO_PINT.get(raw_unit.strip().lower(), raw_unit or "electron_volt")

    element, edge = read_element_edge(h5, scan)
    scale_to_ev = _ENERGY_UNIT_TO_EV.get(unit)
    if element and edge and scale_to_ev:
        try:
            import xraydb

            tabulated_ev = xraydb.xray_edge(element, edge).energy
            values = h5[energy_path][:]
            observed_ev = float(numpy.median(values)) * scale_to_ev
            if tabulated_ev and not (0.1 < observed_ev / tabulated_ev < 10):
                print(
                    f"Warning: {scan!r} energy (units={raw_unit!r} -> {unit!r}) "
                    f"has median {observed_ev:.1f} eV, far from the tabulated "
                    f"{element} {edge} edge ({tabulated_ev:.1f} eV) -- the "
                    "file's 'units' attribute may be wrong; using it as stated.",
                    file=sys.stderr,
                )
        except Exception:
            pass  # best-effort sanity check only; never block on it
    return unit


def _scan_sort_key(scan: str) -> tuple[int, float | str]:
    """Sort BLISS-style numeric scan names ("10.1") numerically; anything
    else (e.g. converted NXxas entry names) falls back to name order."""
    try:
        return (0, float(scan.split(".")[0]))
    except ValueError:
        return (1, scan)


def compatible_exafs_scans(
    input_file: Path,
    energy: str = "Emono",
    signal: str = "mu_trans",
) -> list[str]:
    scans: list[str] = []
    with h5py.File(input_file, "r") as h5:
        for name in h5:
            if not is_nxentry(h5, name):
                continue
            if not is_nxxas_entry(h5, name) and "exafs" not in title_for_scan(h5, name).lower():
                continue
            energy_path = resolve_energy_path(h5, name, energy)
            signal_path = resolve_signal_path(h5, name, signal)
            if energy_path not in h5 or signal_path not in h5:
                continue
            if h5[energy_path].shape != h5[signal_path].shape:
                continue
            scans.append(name)
    return sorted(scans, key=_scan_sort_key)


def scan_points(input_file: Path, scan: str, energy: str = "Emono") -> int:
    with h5py.File(input_file, "r") as h5:
        return int(h5[resolve_energy_path(h5, scan, energy)].shape[0])


def best_compatible_scan(
    input_file: Path,
    energy: str = "Emono",
    signal: str = "mu_trans",
) -> str | None:
    scans = compatible_exafs_scans(input_file, energy=energy, signal=signal)
    if not scans:
        return None
    return max(scans, key=lambda scan: scan_points(input_file, scan, energy))


def list_scans(input_file: Path, energy: str, signal: str) -> None:
    print_banner(f"EXAFS SCANS IN {input_file.name}")
    scans = compatible_exafs_scans(input_file, energy=energy, signal=signal)
    if not scans:
        print("No compatible EXAFS scans found.")
        return
    with h5py.File(input_file, "r") as h5:
        for scan in scans:
            energy_path = resolve_energy_path(h5, scan, energy)
            signal_path = resolve_signal_path(h5, scan, signal)
            definition = entry_definition(h5, scan)
            label = f"{title_for_scan(h5, scan)} [{definition}]" if definition else title_for_scan(h5, scan)
            print(f"  {scan:<8} {label}")
            print(f"           energy: {energy_path} {h5[energy_path].shape}")
            print(f"           signal: {signal_path} {h5[signal_path].shape}")
            available = {**find_detectors(h5, scan), **nxdata_signals(h5, scan)}
            if available:
                print(f"           available signals: {', '.join(sorted(available))}")


def hdf5_input_information(
    input_file: Path,
    scan: str,
    energy: str,
    signal: str,
    energy_unit: str | None = None,
) -> dict[str, str]:
    """Build est's ``input_information`` dict for *scan* in *input_file*.

    *energy_unit* is optional: when omitted (the default), it is auto-detected
    from the resolved energy dataset's own "units" attribute via
    ``resolve_energy_unit`` -- see that function for why a single hardcoded
    unit is wrong across facilities/files. Pass an explicit unit to override.
    """
    input_file = input_file.resolve()

    with h5py.File(input_file, "r") as h5:
        energy_path = resolve_energy_path(h5, scan, energy)
        signal_path = resolve_signal_path(h5, scan, signal)
        missing = [path for path in (energy_path, signal_path) if path not in h5]
        if missing:
            raise ValueError(f"Missing dataset(s) in {input_file}: {', '.join(missing)}")
        if h5[energy_path].shape != h5[signal_path].shape:
            raise ValueError(
                "Energy and signal datasets must have the same shape: "
                f"{energy_path}={h5[energy_path].shape}, "
                f"{signal_path}={h5[signal_path].shape}"
            )
        if energy_unit is None:
            energy_unit = resolve_energy_unit(h5, scan, energy_path)

    return {
        "channel_url": f"silx://{input_file}?{energy_path}",
        "spectra_url": f"silx://{input_file}?{signal_path}",
        "energy_unit": energy_unit,
    }


def packaged_input_information() -> tuple[Path, dict[str, str]]:
    with resources.resource_path("exafs", "EXAFS_Cu.dat") as input_file:
        input_file = Path(input_file)
        return input_file, {
            "channel_url": f"spec://{input_file}?1 cu.dat 1.1 Column 2/Column 1",
            "spectra_url": f"spec://{input_file}?1 cu.dat 1.1 Column 2/Column 2",
            "energy_unit": "electron_volt",
        }


def default_output_file(input_file: Path, scan: str | None) -> Path:
    if scan:
        suffix = scan.replace(".", "_")
        return ROOT / "outputs" / f"{input_file.stem}_{suffix}_result.h5"
    return ROOT / "outputs" / "pymca_exafs_result.h5"


def _scale_energy_to_e0(energy: numpy.ndarray, e0: float | None) -> numpy.ndarray:
    """Rescale *energy* so its magnitude matches *e0*.

    This corrects a genuine est/PyMca inconsistency, not a units bug of ours:
    ``e0`` is found via an internal magnitude heuristic ("these values look
    like keV, not eV" auto-correction) that is *not* reflected back into the
    ``Spectrum.energy`` array est returns -- e.g. for a scan nominally
    spanning ~20 ("electron_volt"), ``e0`` can come back as ~20000 while
    ``energy`` itself stays at ~20 (observed on the BESSY myspot file, with
    the same ``e0`` regardless of which energy_unit was declared at read
    time). Left alone, comparing ``energy`` to ``e0`` (e.g. for the pre/post-
    edge split below) is comparing two different scales.

    Detects the implied power-of-10 correction from the ratio of ``e0`` to
    the array's median and applies it; leaves *energy* untouched when the two
    are already consistent (e.g. every file where est's own energy_unit
    handling isn't tripped by this quirk).
    """
    if e0 is None or energy.size == 0:
        return energy
    median = float(numpy.median(numpy.abs(energy)))
    if median <= 0:
        return energy
    ratio = e0 / median
    if ratio <= 0:
        return energy
    power = round(math.log10(ratio))
    if power == 0:
        return energy
    scale = 10.0**power
    if not (0.5 < ratio / scale < 2.0):
        return energy  # not a clean power-of-10 mismatch; leave as-is
    print(
        f"Note: energy (median {median:.3g}) vs e0 ({e0:.3g}) implies a "
        f"x{scale:g} scale correction (a known est/PyMca quirk: e0 is found "
        "via an internal magnitude heuristic not reflected in the returned "
        "energy array) -- applying it for self-consistency.",
        file=sys.stderr,
    )
    return energy * scale


def write_processed_spectrum(xas_obj, output_file: Path, entry: str) -> bool:
    """Persist the derived EXAFS quantities into *output_file*, as proper
    ``NXdata`` groups under ``"{entry}/process"`` (an ``NXprocess``), with a
    NeXus default-plot chain ending at the Fourier transform.

    est's own ``XASObject.to_file()`` -- what the ``.ows`` graph's final
    "output" node calls -- only ever writes the raw energy/absorbed_beam it
    read in (see ``est/core/io/write_xas.py``); the derived, actually-
    interesting quantities are never serialized by that path. Without this,
    every result file from this pipeline is scientifically empty regardless
    of input file or facility, even though the computation itself is correct.

    Layout::

        {entry}/process/                   NXprocess, @default="fourier_transform"
          energy, k, e0, edge_step           shared axes / scalars
          normalized_mu/                     NXdata vs "energy" (full range)
          pre_edge/                          NXdata vs "energy", sliced to energy <= e0
          post_edge/                         NXdata vs "energy", sliced to energy >= e0
          chi/                               NXdata vs "k"
          fourier_transform/                 NXdata vs "radius" (real/imaginary too)

    est doesn't expose the pre/post-edge fit's own energy windows as metadata
    (checked ``spectrum.pymca_dict``/``xas_obj.configuration``: not present),
    so pre_edge/post_edge are sliced at the one reliably-available boundary,
    ``e0`` -- each curve shown only on its physically meaningful side of the
    edge, rather than PyMca's raw full-range extrapolation.

    Only handles the single-spectrum case (``n_spectrum == 1``), matching this
    demo's scope (every file exercised so far is a single 1D scan); a real
    multi-spectrum map warrants its own N-dimensional NXdata design (est's own
    raw-echo writer keeps a whole map under one NXentry, not one per pixel),
    so it's skipped with a warning rather than guessed at here.
    """
    if xas_obj.n_spectrum != 1:
        print(
            f"Warning: XASObject has {xas_obj.n_spectrum} spectra; "
            "write_processed_spectrum only handles a single spectrum, skipping.",
            file=sys.stderr,
        )
        return False

    spectrum = xas_obj.get_spectrum(0, 0)
    if spectrum.energy is None or spectrum.normalized_mu is None:
        return False

    energy = numpy.asarray(spectrum.energy)
    e0 = float(spectrum.e0) if spectrum.e0 is not None else None
    energy = _scale_energy_to_e0(energy, e0)
    wrote_any = False

    with h5py.File(output_file, "a") as h5:
        process = h5.require_group(f"{entry}/process")
        for name in list(process.keys()):
            del process[name]
        process.attrs["NX_class"] = "NXprocess"
        process.attrs["program"] = "est.core.process.pymca"
        process["energy"] = energy
        if e0 is not None:
            process["e0"] = e0
        if spectrum.edge_step is not None:
            process["edge_step"] = float(spectrum.edge_step)

        def add_nxdata(name: str, signal_name: str, signal, axis_name: str, axis) -> None:
            nonlocal wrote_any
            group = process.require_group(name)
            group.attrs["NX_class"] = "NXdata"
            group.attrs["signal"] = signal_name
            group.attrs["axes"] = axis_name
            group[signal_name] = numpy.asarray(signal)
            if isinstance(axis, h5py.SoftLink):
                group[axis_name] = axis
            else:
                group[axis_name] = numpy.asarray(axis)
            wrote_any = True

        energy_link = h5py.SoftLink(process["energy"].name)
        add_nxdata("normalized_mu", "normalized_mu", spectrum.normalized_mu, "energy", energy_link)

        if spectrum.pre_edge is not None:
            mask = energy <= e0 if e0 is not None else slice(None)
            add_nxdata(
                "pre_edge", "pre_edge", numpy.asarray(spectrum.pre_edge)[mask],
                "energy", energy[mask] if e0 is not None else energy_link,
            )

        if spectrum.post_edge is not None:
            mask = energy >= e0 if e0 is not None else slice(None)
            add_nxdata(
                "post_edge", "post_edge", numpy.asarray(spectrum.post_edge)[mask],
                "energy", energy[mask] if e0 is not None else energy_link,
            )

        if spectrum.chi is not None and spectrum.k is not None:
            process["k"] = numpy.asarray(spectrum.k)
            add_nxdata("chi", "chi", spectrum.chi, "k", h5py.SoftLink(process["k"].name))

        ft = spectrum.ft
        if ft is not None and ft.radius is not None and ft.intensity is not None:
            ft_group = process.require_group("fourier_transform")
            ft_group.attrs["NX_class"] = "NXdata"
            ft_group.attrs["signal"] = "intensity"
            ft_group.attrs["axes"] = "radius"
            ft_group["radius"] = numpy.asarray(ft.radius)
            ft_group["intensity"] = numpy.asarray(ft.intensity)
            if ft.real is not None:
                ft_group["real"] = numpy.asarray(ft.real)
            if ft.imaginary is not None:
                ft_group["imaginary"] = numpy.asarray(ft.imaginary)
            process.attrs["default"] = "fourier_transform"
            h5[entry].attrs["default"] = "process"
            h5.attrs.setdefault("NX_class", "NXroot")
            h5.attrs["default"] = entry  # completes the chain from the file root
            wrote_any = True

    return wrote_any


def execute_est_workflow(
    input_information: dict[str, str],
    input_file: Path,
    output_file: Path,
    scan: str | None = None,
    workflow_name: str = "example_pymca",
) -> dict[str, object]:
    """Run one of est's tutorial ``.ows`` graphs (an Input -> ... -> Output
    ewoks pipeline) on *input_information*.

    *workflow_name* is an est tutorial workflow name, without ``.ows`` (see
    ``est/resources/tutorials``) -- e.g.:

    * ``"example_pymca"`` (default) -- PyMca-based normalization/EXAFS/
      k-weight/FT. Proven against every file this demo has been tested with.
    * ``"example_larch"`` -- xraylarch-based pre_edge/autobk/xftf. **Currently
      fails** on at least the BESSY myspot file (its "autobk" step raises
      "zero-size array to reduction operation minimum" -- an est/larch-side
      issue, not specific to this demo's code). Wired in as an option, but
      treat it as experimental rather than a proven drop-in alternative.

    Either way, the derived spectrum (normalized_mu/pre_edge/post_edge/chi/k/
    fourier_transform) is read off the resulting ``Spectrum`` via
    ``write_processed_spectrum`` -- those field names are backend-agnostic in
    est's model, so no workflow-specific handling is needed there.
    """
    output_file.parent.mkdir(parents=True, exist_ok=True)

    is_pymca = workflow_name == "example_pymca"
    if is_pymca:
        print_workflow_steps()
    else:
        print_banner("EWOKSEST XAS WORKFLOW DEMO")
        print(f"Workflow: {workflow_name}", flush=True)

    with resources.tutorial_workflow(f"{workflow_name}.ows") as workflow:
        print(f"Workflow file : {workflow}")
        print(f"Input data    : {input_file}")
        if scan:
            print(f"Scan          : {scan}")
        print(f"Energy URL    : {input_information['channel_url']}")
        print(f"Signal URL    : {input_information['spectra_url']}")
        print(f"Energy unit   : {input_information['energy_unit']}")
        print(f"Output target : {output_file}")
        print()
        print("Starting ewoks execution...", flush=True)
        started_at = perf_counter()

        inputs = [
            {
                "id": find_node_id(workflow, "ReadXasObject"),
                "name": "input_information",
                "value": input_information,
            },
            {
                "id": find_node_id(workflow, "WriteXasObject"),
                "name": "output_file",
                "value": str(output_file),
            },
        ]

        graph = ows_to_ewoks(str(workflow))
        result = execute_graph(
            graph,
            inputs=inputs,
            outputs=[{"all": True}],
            merge_outputs=True,
        )
        elapsed = perf_counter() - started_at

    xas_obj = result.get("xas_obj")
    wrote_processed = (
        write_processed_spectrum(xas_obj, output_file, xas_obj.entry)
        if xas_obj is not None
        else False
    )

    print_banner("EXECUTION COMPLETE")
    if is_pymca:
        for index, (name, _) in enumerate(WORKFLOW_STEPS, start=1):
            print(f"  [OK] Step {index:02d}: {name}")
        print()
    print(f"Elapsed time : {elapsed:.2f} seconds")
    print(f"Result file  : {result['result']}")
    print(f"File exists  : {'yes' if output_file.exists() else 'no'}")
    if wrote_processed:
        print(f"Processed data (normalized_mu, chi, k, FT): written to {xas_obj.entry}/process")
    else:
        print("Processed data (normalized_mu, chi, k, FT): NOT written (see warnings above)")
    print("=" * 78, flush=True)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run an ewoksest tutorial workflow (--workflow, default "
            "example_pymca) on the packaged sample or on ESRF BLISS/NeXus "
            "HDF5 scans."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="HDF5 file to process. Omit this to run the packaged EXAFS_Cu.dat demo.",
    )
    parser.add_argument("--scan", help="HDF5 scan/entry to process, for example 10.1.")
    parser.add_argument(
        "--energy",
        default="Emono",
        help=(
            "Energy dataset preset or path. Presets: "
            f"{', '.join(ENERGY_PRESETS)}. Automatically resolved to the "
            "right path for NXxas-converted entries too. Default: Emono."
        ),
    )
    parser.add_argument(
        "--signal",
        default="mu_trans",
        help=(
            "Absorption/signal dataset preset or path. Presets: "
            f"{', '.join(SIGNAL_PRESETS)}. Automatically resolved to the "
            "matching NXxas detector (or the processed intensity) for "
            "NXxas-converted entries. Default: mu_trans."
        ),
    )
    parser.add_argument(
        "--energy-unit",
        default=None,
        help=(
            "Energy unit for HDF5 inputs. Default: auto-detected from the "
            "energy dataset's own 'units' attribute (see resolve_energy_unit)."
        ),
    )
    parser.add_argument(
        "--workflow",
        default="example_pymca",
        help=(
            "est tutorial .ows workflow to run, without the extension. "
            "Default: example_pymca (proven). example_larch is wired in but "
            "currently fails on at least the BESSY myspot file -- see "
            "execute_est_workflow's docstring."
        ),
    )
    parser.add_argument("--output", type=Path, help="Output HDF5 file.")
    parser.add_argument(
        "--list-scans",
        action="store_true",
        help="List compatible EXAFS scans in --input and exit.",
    )
    parser.add_argument(
        "--downloaded",
        action="store_true",
        help="Process the best compatible EXAFS scan from each downloaded/*.h5 file.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.list_scans:
        if not args.input:
            raise SystemExit("--list-scans requires --input")
        list_scans(args.input, args.energy, args.signal)
        return

    if args.downloaded:
        files = sorted((ROOT / "downloaded").glob("*.h5"))
        if not files:
            raise SystemExit("No HDF5 files found in downloaded/")
        for input_file in files:
            scan = best_compatible_scan(input_file, args.energy, args.signal)
            if not scan:
                print(f"Skipping {input_file.name}: no compatible EXAFS scans found.")
                continue
            input_information = hdf5_input_information(
                input_file,
                scan=scan,
                energy=args.energy,
                signal=args.signal,
                energy_unit=args.energy_unit,
            )
            try:
                execute_est_workflow(
                    input_information=input_information,
                    input_file=input_file,
                    output_file=default_output_file(input_file, scan),
                    scan=scan,
                    workflow_name=args.workflow,
                )
            except Exception as exc:
                print(
                    f"Failed {input_file.name} scan {scan}: "
                    f"{type(exc).__name__}: {exc}"
                )
        return

    if args.input:
        if not args.scan:
            args.scan = best_compatible_scan(args.input, args.energy, args.signal)
            if not args.scan:
                raise SystemExit(
                    "No compatible EXAFS scans found. Try --list-scans to inspect."
                )
            print(f"No --scan provided; using best compatible scan: {args.scan}")
        input_file = args.input
        input_information = hdf5_input_information(
            input_file,
            scan=args.scan,
            energy=args.energy,
            signal=args.signal,
            energy_unit=args.energy_unit,
        )
        output_file = args.output or default_output_file(input_file, args.scan)
        execute_est_workflow(
            input_information=input_information,
            input_file=input_file,
            output_file=output_file,
            scan=args.scan,
            workflow_name=args.workflow,
        )
        return

    input_file, input_information = packaged_input_information()
    output_file = args.output or default_output_file(input_file, None)
    execute_est_workflow(
        input_information=input_information,
        input_file=input_file,
        output_file=output_file,
        workflow_name=args.workflow,
    )


if __name__ == "__main__":
    main()
