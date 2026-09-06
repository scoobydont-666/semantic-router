"""Extract isolated scenario helpers without changing their test bodies."""
from pathlib import Path
import textwrap

p = Path('tools/pr2067/embedding_init_test.rs')
s = p.read_text()
start = s.index('fn run_case(case: &str) {')
end = s.index('\n#[test]\nfn embedding_init_order_regressions()', start)
old = s[start:end]
markers = [
    ('"factory_first" | "factory_first_direct"', 'assert_factory_first', True),
    ('"mmbert_first" | "mmbert_first_direct"', 'assert_mmbert_first', True),
    ('"concurrent_mmbert"', 'assert_concurrent_mmbert', False),
    ('"concurrent_other_factory"', 'assert_concurrent_other_factory', False),
    ('"failed_load_retry" | "failed_standalone_retry"', 'assert_failed_load_retry', True),
    ('"poisoned_gate"', 'assert_poisoned_gate', False),
]
helpers = []
dispatch = old
for pattern, name, uses_case in markers:
    opening = f'        {pattern} => {{\n'
    if old.count(opening) != 1:
        raise RuntimeError(f'Expected one scenario {pattern}')
    a = old.index(opening)
    b = old.index('\n        }\n', a) + len('\n        }\n')
    arm = old[a:b]
    body = textwrap.dedent(arm[len(opening):-len('        }\n')])
    argument = 'case: &str' if uses_case else ''
    helpers.append(f'fn {name}({argument}) {{\n' + textwrap.indent(body, '    ') + '}\n')
    call = 'case' if uses_case else ''
    dispatch = dispatch.replace(arm, f'        {pattern} => {name}({call}),\n')
new = '\n'.join(helpers) + '\n' + dispatch
p.write_text(s[:start] + new + s[end:])
print('Extracted six scenario helpers; preserved all test bodies.')
