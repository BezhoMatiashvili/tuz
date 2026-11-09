# Public oil and filter connectors

Observed 2026-11-09 from the pages' own scripts and confirmed with live reads.
`reverse-api-engineer` 0.14.0 was installed. A headless capture of Partsouq did not
produce a client: the default Claude model returned a weekly limit, and the isolated
OpenCode fallback returned a free-usage limit. No HAR or generated client is claimed.
The connectors below follow the same public calls those pages make. Cookies, page
tokens and captured traffic are not stored in this repository.

Every result stays provisional. Supplier price, stock and approval text are not this
warehouse's stock or a verified oil approval. `coverage_complete` stays false.

## What a VIN search calls

Fuchs, Shell and Pakelo receive the decoded make, model and year. They do not receive
the VIN. JS Asakashi receives the VIN on its public VIN form. If more than one engine
matches, the source is `CONFLICTED`, the clarification lists the engine names, and no
product is returned.

| Source | Call | Live check |
|---|---|---|
| Fuchs | `GET https://fuchs.webbhuset.com/_api/{categories,makes,models,types,recommendations}` with `x-oly-dataset: fuchs_eu` and an empty `x-oly-subscription`, which is what the public widget sends | Cars category, Honda make and an Accord engine returned named TITAN engine oils |
| Shell | `GET https://d126xd59kaz5rr.cloudfront.net/api/proxy/oats/shell-lmc-gb/search` and `/equipment/{id}` for the public en-GB car chooser | Honda search returned equipment with year range and engine products such as Helix |
| Pakelo | Same OATS shape at `https://pakelo-wsc-it.phoenix.earlweb.net`, plus a per-lookup read of the public `token` attribute on the find-your-oil page | Search returned equipment JSON only when that page token was supplied. The token is not saved |
| JS Asakashi | `POST https://www.jsfilter.jp/application/get_applications_by_vin/` form field `vin_number`; HTML columns OIL, AIR, FUEL, CABIN, TRANS | A public sample VIN returned recognition text and no filter table, so the client returns empty. A catalogue table with one data row returns its filter codes |

Disable a source with `FUCHS_ENABLED`, `SHELL_ENABLED`, `PAKELO_ENABLED` or
`JSFILTER_ENABLED` set to `false`.

## Catalogs left disconnected

These were requested and checked. No client is enabled, because a guessed endpoint
would not be evidence:

- Partsouq answered the anonymous fetch with a challenge page.
- Emex did not expose a replayable anonymous search call; a guessed form post was rejected.
- Amayama returned script addresses that are not executable bundles.
- Megazip renders its catalog in the browser; the homepage did not expose a VIN parts call
  that could be replayed and checked.

A later capture can add one of these only after its own response contains the vehicle
and a part number from that site.
