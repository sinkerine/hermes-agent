# Element Web interactive UI v1 contract

Source: https://github.com/element-hq/element-web, commit
`bcb080171cb312feef37e63007c7ca8cc0a8b77f`.
Source directory: `apps/web/src/interactive-ui/` (schema and entire fixtures directory).
The reference checkout was read only. These JSON files and fixture README are unmodified copies.

Copyright 2026 sinkerine.
Original SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Element-Commercial.
The copyright holder additionally grants the MIT license below for these vendored copies
and the Python port of `protocol.ts` in this package (`protocol*.py`).
The original SPDX notices remain in the copied files to record their provenance.

The executable source parser and fixtures govern behavior where prose disagrees.
The Python entry point accepts a payload directly; the Web entry point unwraps the
`net.15cm.matrix.interactive_ui` event property first. Producer mode adds validation
after client validation, without changing client error precedence. In particular,
`fixtures/valid/content.json` intentionally contains the reachable `future_widget`
node: clients accept/degrade it, while strict producers reject `nodes[9].type`
with `bad_type`. This exception to producer acceptance is intentional; the fixture
is preserved byte for byte.

## MIT License

Copyright (c) 2026 sinkerine

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## URL conformance limit

The Python port uses `idna`'s public UTS46 mapping/context/bidi APIs. This is
conservative for obscure Unicode hostnames; it is not a complete WHATWG engine.
A seeded probe found 8 rejections among 1500 hosts which Node accepts. The
supervisor accepted retaining these rejections in both parser modes. Regression
cases live in `test_protocol_url.py`; protocol fixtures remain byte-identical.

Probe environment: Python 3.14.9 (Unicode 16.0.0), idna 3.20 (Unicode 18.0.0),
Node 24.21.0 (ICU 78.3, Unicode 17.0, ada 4.0.0). Seed: Python `Random(42)`;
each host consists of three `chr(r.randrange(128, 0x10000))` characters plus
`.com`, repeated 1500 times. Node accepts all eight hosts below; Python rejects:

- `\u1ad0\u5b0a\uc835.com`
- `\u0879\u1012\u7b1e.com`
- `\u3cfc\u1ad1\u25fd.com`
- `\u1ac3\u14ff\u5472.com`
- `\u08ca\u9b67\u27c7.com`
- `\ufbcf\u247f\u7aba.com`
- `\u8a8f\u228b\ua7ce.com`
- `\u089c\u186f\u6840.com`

Reproduce with the PM test interpreter as `python` and Node on PATH, from the
checkout root (the command compares actual URL execution, not source text):

```bash
python - <<'PY'
import json, random, subprocess
from plugins.platforms.matrix.interactive_ui.protocol_schema import valid_https
r = random.Random(42)
urls = ['https://' + ''.join(chr(r.randrange(128, 0x10000)) for _ in range(3)) + '.com' for _ in range(1500)]
js = 'const u=JSON.parse(require("fs").readFileSync(0,"utf8")); console.log(JSON.stringify(u.map(s=>{try{const x=new URL(s);return !!x.hostname&&!x.username&&!x.password}catch{return false}})))'
result = subprocess.run(['node', '-e', js], input=json.dumps(urls), text=True, capture_output=True, check=True, timeout=30)
for url, expected in zip(urls, json.loads(result.stdout), strict=True):
    if valid_https(url) != expected:
        print(ascii(url), expected, valid_https(url))
PY
```
