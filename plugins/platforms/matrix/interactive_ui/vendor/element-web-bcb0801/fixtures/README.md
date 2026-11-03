<!--
Copyright 2026 sinkerine

SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Element-Commercial
Please see LICENSE in the repository root for full details.
-->

# Interactive UI v1 conformance fixtures

These files contain payloads, not complete Matrix events. Wrap each payload in
`{ "net.15cm.matrix.interactive_ui": payload }` before calling the web parser.
`valid/*.json` must parse successfully; each `invalid/*.json` has a sibling
`*.expected.json` specifying the first `{ "path", "code" }` error. Paths are relative
to the payload. Array indices use brackets. The empty path denotes the whole payload.

Hermes and Android should vendor this directory together with
`../schema/interactive-ui-v1.schema.json`, retaining the licence and recording the
Element Web source commit in their own repository. Copy the entire set together;
do not silently edit individual fixtures downstream. Run all fixtures after updating.
The schema describes JSON shapes; byte limits, tree references, state compatibility,
chart dimensions and other relational constraints also require semantic validation.

The examples cover layout, content, controls, form, table, four chart kinds, English
and Chinese knowledge maps, revisions, heat-pump suitability, parcel comparison,
product and event carousels, venue comparison, quiz, six-step diagram, recipe
checklist and a text/link itinerary fallback. Media references and cryptographic
fields are synthetic test data; they do not identify usable encrypted attachments.

## Contract resolutions

- Clients accept at most 60000 UTF-8 JSON bytes; Hermes producers use 40000. The cap
  includes ignored properties. Tests generate the exact client boundary separately.
- Root depth is one. Sharing a child produces `duplicate_id`; a back reference to
  an ancestor produces `cycle`. Unreachable nodes are omitted from the returned
  payload/map, but malformed node structures still fail validation. State type
  inference, bindings, conditions and actions use only that retained tree. Error
  paths keep the original wire indices even when other nodes are discarded.
- Unknown node kinds become `type: "unknown"` with their id, condition and optional
  fallback. Unknown properties are omitted from parsed output.
- The approved null-state ruling derives the expected string or number type from
  control bindings when its initial value is null. Null boolean/array controls and
  contradictory bindings are invalid. Required text fields reject null at submission.
- Saved state accepts either the persistence envelope `{ ui_id, revision, state }`
  or a previous state map. An envelope has all three own properties and an
  object-valued (non-null, non-array) `state`. Other objects are raw state maps,
  including maps whose legal keys are `ui_id`, `revision` or `state`. Envelopes
  from another UI, a future revision or with invalid metadata are ignored.
- Strings use Unicode code-point lengths after trimming. State strings allow up to
  each retained text input's `max_length` (up to 2000); unbound strings default
  to 200. Initial values, saved values, set-state actions and submissions share
  these limits. Discarded text inputs do not raise an unbound key's limit.
- Scatter's total-values budget counts both coordinates (at most 400 points across
  series); categorical charts count individual values (at most 800).
- Images require exactly one mxc URL or encrypted file. Encrypted files require v2,
  A256CTR/oct, a 256-bit key and SHA-256 digest, and a 128-bit IV in Matrix encodings.
  Image dimensions must be positive signed 32-bit integers.
- Carousel items have one optional `action` object; an array in that field is invalid.
  Unknown properties (including a plural `actions` field) are ignored.
- `addReplyToMessageContent` creates the reply mixin only. The action builder adds
  the originating thread relation before calling that helper. MatrixEvent.getId()
  continues to refer to the original event after replacement.
- Graphs sharing `bind_selected` allow the union of their graph node ids, or null.
  A branch with no matching local id shows no selection/detail. Revisions retain
  an id only while some graph bound to the key still contains it.

`validateSubmission(payload, nodeId, state, carouselItemId?)` also validates a
carousel item's submit action when both the carousel node id and explicit item id
are supplied. Buttons and items share include filtering and value checks. Missing
or incorrect item targets fail validation; a fourth argument on a non-carousel
node also fails.

## Running conformance tests

From the repository root, prepare workspace build outputs and run:

```sh
pnpm nx run-many -t test:unit:prepare -p
pnpm vitest run apps/web/src/interactive-ui
```

The tests check the schema's exact catalog/property inventory, required fields,
shared references and bounds. These are agreement assertions, not independent JSON
Schema execution. All valid/invalid fixtures also run through the parser; invalid
fixtures must match their expected paths/codes. Tests mutate fixtures with a seeded
random generator, and measure the exact 60000-byte/300-node boundary. Non-JSON
inputs (cycles, accessors, sparse arrays, non-finite numbers) are generated in tests
because JSON fixture files cannot encode them. Error precedence is byte/JSON safety,
then schema shape/limits in wire property order, then semantic references and tree
checks. Use the expected files as the authoritative first-error examples.
