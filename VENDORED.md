# Vendored code

These modules come from [checkrein](https://github.com/kreativlogics/checkrein) (same author, MIT). The copies are
verbatim unless marked adapted. A bug fixed here is fixed in checkrein too, and the other way round.

| berth file | checkrein source | sha256 at copy | Status |
|---|---|---|---|
| `plugins/berth/scripts/berth/shell.py` | `plugins/checkrein/scripts/checkrein/shell.py` | `a43e6ed8230cd3827a6318660a465b282ff05df5716b568fb4cb18b684755a34` | adapted: program names read in lower case (`_base`), `arch`, `xcrun` and `script` unwrapped as wrappers (`_script_command`), `env -S` read at the end of a cluster (`_env_split_at`) and split as env splits it, `\_` and quotes included (`_env_split`; a string it cannot read exactly flags an ask problem), env's `-P`, `-a` and `--argv0` read as taking a value, and its unambiguous long-option prefixes expanded (`_env_long`) and wrapper option clusters read (`_wrapper_option_width`); reported upstream. checkrein's copy has changed since and is re-vendored when its rewrite settles |
| `plugins/berth/scripts/berth/context.py` | `plugins/checkrein/scripts/checkrein/context.py` | `5f2e8407aca79375351120d19fde4d52c697298b4945c92f8916c704c6de2e8a` | verbatim from checkrein as of this release, plus `--shallow-file` in `_GIT_GLOBAL_VALUE` (also fixed in checkrein); checkrein has changed since (multi-folder `cwds`, brace budget) and is re-vendored when that settles |
| `plugins/berth/scripts/berth/globs.py` | `plugins/checkrein/scripts/checkrein/globs.py` | `42fe1796f6927f23306735de89a2d9c3e73e0d1bbe65bc319ef027c65a067495` | verbatim (needed by context.py) |
| `plugins/berth/scripts/berth/approve.py` | `plugins/checkrein/scripts/checkrein/approve.py` | `e96625250a49e368d20675e5ffce26329fa0e2ddd23dbd691808970d2b1eb948` | verbatim |
| `plugins/berth/scripts/berth/secrets.py` | `plugins/checkrein/scripts/checkrein/secrets.py` | `ac97b456ebe244497e45d30df887b00ae9c176fd3e6b9a7eab6cd99606aed019` | verbatim |
| `plugins/berth/scripts/berth/cfg.py` | `plugins/checkrein/scripts/checkrein/cfg.py` | `f08bc0b06072545dc088adf768da0a6cae34caec4e92cb7729720bbde78304cf` | adapted: checkrein's layering rules (safety order, union, minimum, user-only, approved) with berth's schema, presets and validation |

To check a verbatim copy: `shasum -a 256 <file>` must print the sha256 above. When checkrein's copy changes, copy it
again, run the tests, and update the hash here.
