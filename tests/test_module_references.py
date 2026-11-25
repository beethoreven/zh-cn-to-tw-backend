"""Every `module.name` reference in the backend must resolve: walk each project .py
with ast, find Attribute nodes whose value is a Name bound to an imported module,
import that module and check hasattr. Uses the test harness so the real .env is
never loaded."""
import ast, importlib, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401
ROOT = harness.BACKEND
SKIP_DIRS = {"venv", ".venv", "__pycache__", ".git", "uploads", "outputs", "node_modules", "tests", "build", "dist"}

def module_name(path):
    rel = os.path.relpath(path, ROOT)[:-3].replace(os.sep, ".")
    return rel[: -len(".__init__")] if rel.endswith(".__init__") else rel

def resolve_relative(cur_mod, is_pkg, level, target):
    parts = cur_mod.split(".")
    base = parts if is_pkg else parts[:-1]
    base = base[: len(base) - (level - 1)] if level > 1 else base
    return ".".join([p for p in base + ([target] if target else []) if p])

def is_module(name):
    try:
        mod = importlib.import_module(name)
        return mod
    except Exception:
        return None

files = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
    files += [os.path.join(dirpath, f) for f in filenames if f.endswith(".py")]

total_refs, problems, unimportable = 0, [], set()
for path in sorted(files):
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    cur = module_name(path)
    is_pkg = path.endswith("__init__.py")
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            base = resolve_relative(cur, is_pkg, node.level, node.module) if node.level else node.module
            for a in node.names:
                if a.name == "*":
                    continue
                full = f"{base}.{a.name}" if base else a.name
                if is_module(full) is not None and full not in sys.builtin_module_names:
                    # only treat it as a module alias if it really is a module, not a function/class
                    mod = sys.modules.get(full)
                    if mod is not None and type(mod).__name__ == "module":
                        aliases[a.asname or a.name] = full
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
            target = aliases[node.value.id]
            mod = is_module(target)
            if mod is None:
                unimportable.add(target)
                continue
            total_refs += 1
            if not hasattr(mod, node.attr):
                problems.append(f"{os.path.relpath(path, ROOT)}:{node.lineno}  {node.value.id}.{node.attr}  ({target} has no '{node.attr}')")

print(f"checked {total_refs} module.name references across {len(files)} files")
print("unimportable modules (skipped):", sorted(unimportable) or "none")
print("\n".join(problems) if problems else "no unresolved references")
sys.exit(1 if problems else 0)
