"""Select numerical tolerances from explicit upstream assertions or declared precision."""
import ast
import hashlib
from pathlib import Path

from .numeric import validate_numeric_policy

DEFAULTS = {'float16': (1e-3, 1e-5), 'float32': (1e-5, 1e-7), 'complex64': (1e-5, 1e-7),
            'float64': (1e-9, 1e-12), 'complex128': (1e-9, 1e-12)}


def upstream_tolerances(source_path, symbol):
    """Inspect bounded adjacent test files; only direct assertions on the selected symbol count."""
    source_path = Path(source_path)
    if not source_path.is_file() or source_path.is_symlink():
        return []
    paths = [source_path]
    for parent in list(source_path.parents)[:3]:
        paths.extend((parent / ('test_' + source_path.stem + '.py'),
                      parent / (source_path.stem + '_test.py'),
                      parent / 'tests' / ('test_' + source_path.stem + '.py')))
    found = []
    for path in dict.fromkeys(paths):
        if (path.suffix != '.py' or path.is_symlink() or not path.is_file()
                or path.stat().st_size > 512 * 1024
                or any(parent.is_symlink() for parent in path.parents)):
            continue
        data = path.read_bytes()
        try:
            tree = ast.parse(data)
        except (SyntaxError, ValueError):
            continue
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for name in node.names:
                    aliases[name.asname or name.name.split('.')[0]] = name.name if name.asname else name.name.split('.')[0]
            elif isinstance(node, ast.ImportFrom) and node.module:
                for name in node.names:
                    aliases[name.asname or name.name] = node.module + '.' + name.name

        def qualified(node):
            if isinstance(node, ast.Name):
                return aliases.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                return qualified(node.value) + '.' + node.attr
            return ''

        def selected_call(node):
            if not isinstance(node, ast.Call):
                return False
            name = qualified(node.func)
            suffix = source_path.stem + '.' + symbol
            return (name == suffix or name.endswith('.' + suffix)
                    or (path == source_path and name == symbol and symbol not in aliases))

        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        def scope(node):
            while node in parents:
                node = parents[node]
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    return node
            return tree

        assignments = sorted((node for node in ast.walk(tree)
                              if isinstance(node, (ast.Assign, ast.AnnAssign))), key=lambda node: node.lineno)

        def dependencies(assertion):
            names = set()
            constants = {}
            selected_scope = scope(assertion)
            for node in assignments:
                if node.lineno >= assertion.lineno:
                    continue
                own_scope = scope(node)
                if own_scope not in (tree, selected_scope):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                declared = {item.id for target in targets for item in ast.walk(target) if isinstance(item, ast.Name)}
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    value = None
                for name in declared:
                    constants[name] = value
                if own_scope != selected_scope:
                    continue
                linked = any(selected_call(item) or isinstance(item, ast.Name) and item.id in names
                             for item in ast.walk(node.value)) if node.value else False
                names.difference_update(declared)
                if linked:
                    names.update(declared)
            return names, constants

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if qualified(node.func) != 'numpy.testing.assert_allclose':
                continue
            names, constants = dependencies(node)
            if not any(selected_call(call) or isinstance(call, ast.Name) and call.id in names
                       for argument in node.args[:2] for call in ast.walk(argument)):
                continue
            values = {'rtol': 1e-7, 'atol': 0.0, 'equal_nan': True}
            for keyword in node.keywords:
                if keyword.arg in ('rtol', 'atol', 'equal_nan'):
                    try:
                        values[keyword.arg] = (constants[keyword.value.id] if isinstance(keyword.value, ast.Name)
                                               else ast.literal_eval(keyword.value))
                    except (ValueError, TypeError):
                        raise ValueError('upstream numerical tolerance requires explicit resolution') from None
                    except KeyError:
                        raise ValueError('upstream numerical tolerance requires explicit resolution') from None
            policy = {'kind': 'float-tolerance', 'equal_nan': True, **values}
            validate_numeric_policy(policy)
            found.append((policy, {'file': path.name, 'line': node.lineno,
                                   'sha256': hashlib.sha256(data).hexdigest()}))
    return found


def select_numeric_policy(material):
    contracts = upstream_tolerances(material.source_path, material.symbol)
    if contracts:
        policy = contracts[0][0]
        if any(other != policy for other, _evidence in contracts[1:]):
            raise ValueError('conflicting upstream numerical tolerances require a per-case contract')
        return dict(policy, basis='upstream assertion', evidence=[item for _policy, item in contracts])
    dtypes = [param.dtype for param in material.schema.params if param.dtype in DEFAULTS]
    dtype = max(dtypes, key=lambda value: DEFAULTS[value][0]) if dtypes else 'float64'
    rtol, atol = DEFAULTS[dtype]
    return {'kind': 'float-tolerance', 'rtol': rtol, 'atol': atol, 'equal_nan': True,
            'basis': 'declared-precision default', 'precision': dtype}
