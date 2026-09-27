| Directory | |
| --- | --- |
| [ticket/](ticket/) | Support message, refund / intent / frustration |
| [code-change/](code-change/) | A skill hunk, whether the change needs real code in state |
| [fan-out/](fan-out/) | One operation Choice plus speculative `*_target` heads |

```bash
WORKDIR="${TMPDIR:-/tmp}/codex/typesafeai-cli"
mkdir -p "$WORKDIR"
cp examples/ticket/*.json "$WORKDIR/"
typesafe ask --state-file "$WORKDIR/state.json" --questions-file "$WORKDIR/questions.json"
```
