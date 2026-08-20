#!/usr/bin/env python3
"""Страж дрейфа синхронизированной копии чат-панели: app.js ↔ inbox.js.

app.js (виджет сделки) и inbox.js («Чаты») делят общий набор методов
чат-панели — копия без JS-тестов (см. ponytail-маркеры в шапках обоих
файлов). Ручная синхронизация уже однажды дрейфала; этот скрипт —
минимальная страховка:

- ``same`` (идентичные методы): тела обязаны совпадать — дрейф = ошибка.
- ``diff`` (намеренно расходящиеся: stale-гарды, a11y, scroll-anchor,
  supervisor-режим inbox): заморожены хэши обеих сторон; изменение любой
  стороны = «проверь парную копию: синхронизировать или обновить базу».
- Новый общий метод вне базы = ошибка: классифицируй через --update-baseline.
- ``node --check`` на app.js/inbox.js и инлайн-скрипты admin.html
  (инцидент 2026-08-20: кавычка в инлайн-JS убила весь скрипт панели —
  pytest/ruff JS не видят; нет node в окружении — проверка тихо скипается).

Baseline (``scripts/js_sync_baseline.json``) коммитится. После осознанного
изменения: ``python scripts/check_js_sync.py --update-baseline``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "src/app/static/app.js"
INBOX = REPO / "src/app/static/inbox.js"
BASELINE = Path(__file__).resolve().parent / "js_sync_baseline.json"

# Методы объекта Alpine-компонента: ровно 4 пробела отступа, «name(» или
# «async name(». Вложенные объявления глубже — не матчатся.
_METHOD_RE = re.compile(r"^    (?:async )?([a-zA-Z_]\w*)\(", re.MULTILINE)


def _extract_methods(src: str) -> dict[str, str]:
    """Имя метода → нормализованное тело (от «{» до балансной «}»).

    Экстрактор одинаков для обоих файлов, поэтому его систематические
    неточности (фигурные скобки внутри строк) взаимно сокращаются при
    сравнении на равенство.
    """
    out: dict[str, str] = {}
    for m in _METHOD_RE.finditer(src):
        name = m.group(1)
        i = src.index("{", m.start())
        depth = 0
        k = i
        while k < len(src):
            if src[k] == "{":
                depth += 1
            elif src[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        body = "\n".join(line.rstrip() for line in src[i : k + 1].splitlines()).strip()
        out.setdefault(name, body)
    return out


def _sha(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def _node_check(sources: list[tuple[str, str]]) -> list[str]:
    """``node --check`` на JS-источники (файлы и инлайн-скрипты)."""
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        print("node не найден — синтаксис JS не проверен", file=sys.stderr)
        return []
    errors: list[str] = []
    for name, src in sources:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as f:
            f.write(src)
            tmp = f.name
        try:
            r = subprocess.run(
                [node, "--check", tmp], capture_output=True, text=True, check=False
            )
        finally:
            Path(tmp).unlink(missing_ok=True)
        if r.returncode != 0:
            lines = (r.stderr or "").strip().splitlines()
            errors.append(f"syntax: {name} — {lines[-1] if lines else 'node --check failed'}")
    return errors


def _classify(app: dict[str, str], inbox: dict[str, str]) -> dict:
    common = sorted(set(app) & set(inbox))
    same = [n for n in common if app[n] == inbox[n]]
    diff = {
        n: {"app": _sha(app[n]), "inbox": _sha(inbox[n])}
        for n in common
        if app[n] != inbox[n]
    }
    return {"same": same, "diff": diff}


def check(app: dict[str, str], inbox: dict[str, str], baseline: dict) -> list[str]:
    errors: list[str] = []
    common = sorted(set(app) & set(inbox))
    same_set, diff_map = set(baseline["same"]), baseline["diff"]
    for name in common:
        if name in same_set:
            if app[name] != inbox[name]:
                errors.append(f"drift: {name} — тела разошлись; синхронизируй app.js и inbox.js")
        elif name in diff_map:
            side = diff_map[name]
            for file, body, frozen in (("app.js", app[name], side["app"]), ("inbox.js", inbox[name], side["inbox"])):
                if _sha(body) != frozen:
                    errors.append(
                        f"changed: {file}:{name} — расходящийся метод изменён; проверь парную "
                        f"копию (синхронизировать или --update-baseline)"
                    )
        else:
            errors.append(f"new: {name} — новый общий метод; классифицируй (--update-baseline)")
    for name in sorted((same_set | set(diff_map)) - set(common)):
        errors.append(f"stale: {name} — метод исчез из файлов; --update-baseline")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("app", nargs="?", default=APP, type=Path, help=APP.name)
    parser.add_argument("inbox", nargs="?", default=INBOX, type=Path, help=INBOX.name)
    parser.add_argument("--update-baseline", action="store_true", help="перезаписать baseline")
    args = parser.parse_args()

    app_src = args.app.read_text(encoding="utf-8")
    inbox_src = args.inbox.read_text(encoding="utf-8")
    admin_html = (REPO / "src/app/static/admin.html").read_text(encoding="utf-8")
    inline = re.findall(r"<script>(.*?)</script>", admin_html, re.DOTALL)
    syntax_errors = _node_check(
        [
            ("app.js", app_src),
            ("inbox.js", inbox_src),
            *[(f"admin.html<script#{i}>", s) for i, s in enumerate(inline, 1)],
        ]
    )

    app = _extract_methods(app_src)
    inbox = _extract_methods(inbox_src)
    state = _classify(app, inbox)

    if syntax_errors:
        print(f"js syntax: {len(syntax_errors)} проблем(а)", file=sys.stderr)
        for e in syntax_errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    if args.update_baseline:
        BASELINE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"baseline обновлён: {len(state['same'])} same, {len(state['diff'])} diff → {BASELINE.name}")
        return 0

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    errors = check(app, inbox, baseline)
    if errors:
        print(f"js sync: {len(errors)} проблем(а)", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    print(f"js sync ok: {len(state['same'])} same, {len(state['diff'])} diff (baselined)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
