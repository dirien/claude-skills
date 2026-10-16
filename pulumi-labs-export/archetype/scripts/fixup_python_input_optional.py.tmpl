"""Rewrite `pulumi.Input[Optional[X]]` -> `Optional[pulumi.Input[X]]`.

Workaround for https://github.com/pulumi/pulumi/issues/23414. The Python
codegen used by `pulumi package gen-sdk` emits the inner-Optional nesting
for optional non-plain inputs, but the runtime's `_types.unwrap_type` only
recognizes the outer-Optional form. Serializing a list or dict value to
such a property trips `_get_list_element_type` in `pulumi.runtime.rpc`.
This script mechanically flips the nesting in every generated `.py` file
under the given root. It is idempotent: rewritten files no longer contain
the needle, so reruns are no-ops.

Usage:
    python3 scripts/fixup_python_input_optional.py sdk/python
"""

import pathlib
import sys

NEEDLE = "pulumi.Input[Optional["


def fix(text: str) -> str:
    out: list[str] = []
    i = 0
    while True:
        j = text.find(NEEDLE, i)
        if j < 0:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:j])
        # Two `[` are already open at this point (from `Input[Optional[`).
        # Walk forward, balancing brackets until both close.
        depth = 2
        k = j + len(NEEDLE)
        while depth > 0 and k < len(text):
            c = text[k]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
            k += 1
        if depth != 0:
            out.append(text[j:])
            return "".join(out)
        inner = text[j + len(NEEDLE):k - 2]
        out.append("Optional[pulumi.Input[" + inner + "]]")
        i = k


def main() -> None:
    root = pathlib.Path(sys.argv[1])
    for path in root.rglob("*.py"):
        s = path.read_text()
        if NEEDLE in s:
            path.write_text(fix(s))


if __name__ == "__main__":
    main()
