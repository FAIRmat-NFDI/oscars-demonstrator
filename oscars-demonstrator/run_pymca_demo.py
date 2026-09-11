#!/usr/bin/env python3
"""Run the ewoksest PyMca EXAFS tutorial workflow headlessly."""

from __future__ import annotations

import argparse
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
    energy_unit: str,
) -> dict[str, str]:
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


def execute_pymca_workflow(
    input_information: dict[str, str],
    input_file: Path,
    output_file: Path,
    scan: str | None = None,
) -> dict[str, object]:
    output_file.parent.mkdir(parents=True, exist_ok=True)

    print_workflow_steps()

    with resources.tutorial_workflow("example_pymca.ows") as workflow:
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

    print_banner("EXECUTION COMPLETE")
    for index, (name, _) in enumerate(WORKFLOW_STEPS, start=1):
        print(f"  [OK] Step {index:02d}: {name}")
    print()
    print(f"Elapsed time : {elapsed:.2f} seconds")
    print(f"Result file  : {result['result']}")
    print(f"File exists  : {'yes' if output_file.exists() else 'no'}")
    print("=" * 78, flush=True)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the ewoksest PyMca tutorial workflow on the packaged sample "
            "or on ESRF BLISS/NeXus HDF5 scans."
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
        default="kiloelectron_volt",
        help="Energy unit for HDF5 inputs. Default: kiloelectron_volt.",
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
                execute_pymca_workflow(
                    input_information=input_information,
                    input_file=input_file,
                    output_file=default_output_file(input_file, scan),
                    scan=scan,
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
        execute_pymca_workflow(
            input_information=input_information,
            input_file=input_file,
            output_file=output_file,
            scan=args.scan,
        )
        return

    input_file, input_information = packaged_input_information()
    output_file = args.output or default_output_file(input_file, None)
    execute_pymca_workflow(
        input_information=input_information,
        input_file=input_file,
        output_file=output_file,
    )


if __name__ == "__main__":
    main()
