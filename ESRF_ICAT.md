# ESRF ICAT+ API — endpoints used, what's supported, and a short wishlist

This document collects notes from the OSCARS XAS demonstrator on how we drive ESRF discovery/download through the public **ICAT+** REST API, and a few things that would improve technique-based discovery.

The actual implementation can be found in the [`nomad-semantic-web-service`](https://github.com/FAIRmat-NFDI/nomad-semantic-web-service/blob/main/src/nomad_semantic_web_service/catalogue/icat.py) package.

Base URL: `https://icatplus.esrf.fr` (Swagger at `/swagger.json`). Every call below is made **anonymously** — no ESRF account — which ICAT+ allows for public (post-embargo) data, and which the anonymous IDS download path makes possible.

## 1. Endpoints we actually use

| Endpoint | Params we send | Role in the demo |
|---|---|---|
| `GET /catalogue/datasets` | `startDate`, `endDate`, `instrumentName`, `limit`, `sortBy=STARTDATE`, `sortOrder` (also accepts `techniquePids`, `investigationIds`, `datasetType`) | **Primary dataset listing** and the route the pipeline lists candidates with. |
| `GET /ids/data/download` | `datasetIds` (or `datafileIds`), `inline=false` | **Anonymous download.** Redirects to `ids.esrf.fr/ids/getData?sessionId=…`. |
| `GET /ids/{sessionId}/datasets/status` | `datasetIds` | Online status (`ONLINE` / `ARCHIVED` / …). We check if the data is online or on tape before downloading. |
| `POST /ids/{sessionId}/datasets/restore` | `datasetIds` | Request a tape restore (async). |
| `GET /catalogue/{sessionId}/dataset/id/{id}/datafile` | — | List a dataset's individual files, to download only `.h5` instead of the whole set. |
| `GET /facilities` | — | Facility metadata; the service reads it to confirm ESRF advertises **ESRFET** for technique terms. |

Public landing pages are resolved from each record's `investigation.doi` via
`https://doi.org/<doi>` (not the internal `location` storage path).

> `/map` (PaNET→ESRFET) is **our** `nomad-semantic-web-service` endpoint, not
> ESRF's — mentioned only to avoid confusion.

## 2. Technique search — supported by the API

Filtering by technique filter can be done on the `GET /catalogue/datasets` route. It accepts a `techniquePids` query parameter and ICAT+ applies it server-side.

The actual gap is **annotation**: public dataset records from ESRF's BM23 beamline come back with an empty `techniques: []` array (and a dataset `parameters` entry `definition = UNKNOWN`). The filter works, it just has nothing to match. If we search the live server for beamline BM23:

| query | result |
|---|---|
| `…/catalogue/datasets?…&instrumentName=BM23` | **5** datasets |
| `…/catalogue/datasets?…&instrumentName=BM23&techniquePids=XAS` | **0** datasets |
| same with `techniquePids=EXAFS` or `…/PaNET01196` | **0** datasets |

So, adding the technique filter gives us no datasets. The unannotated datasets are correctly filtered *out*, confirming the filter is working and the records simply lack the PIDs.

Consequence for the demo at the moment: we use ID21 (fluorescence-yield µXANES) datasets which *are* annotated with the XAS technique PID, so the resolved ESRFET XAS IRI filters them **server-side**, and they are kept on disk. Once the records carry technique PIDs and are restored, the same `/catalogue/datasets` could be applied to BM23 data (with `techniquePids=…`) and filter by technique directly.