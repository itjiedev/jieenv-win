_registry = []


def register(info: dict):
    if info not in _registry:
        _registry.append(info)


def list_all() -> list:
    return sorted(_registry, key=lambda x: x.get('sort', 0))


def list_by_category(category: str) -> list:
    return sorted(
        [t for t in _registry if t.get('category') == category],
        key=lambda x: x.get('sort', 0),
    )


def list_categories() -> dict:
    cats = {}
    for t in list_all():
        c = t.get('category', '')
        if c not in cats:
            cats[c] = []
        cats[c].append(t)
    return cats


def grouped_by_order(category_order, default_key):
    groups = {}
    for t in list_all():
        c = t.get('category') or default_key
        groups.setdefault(c, []).append(t)
    ordered = []
    for key in category_order:
        if key in groups:
            ordered.append({'key': key, 'tools': groups.pop(key)})
    for key in list(groups.keys()):
        ordered.append({'key': key, 'tools': groups.pop(key)})
    return ordered


def get(tool_id: str) -> dict | None:
    for t in _registry:
        if t.get('tool_id') == tool_id:
            return t
    return None


def reset():
    _registry.clear()