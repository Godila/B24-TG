"""Мини-страницы публичных роутов (единый брендированный шаблон).

Наследники _UNREGISTERED_PAGE из app.py: одна карточка, лого, title+body.
Параметризация str.format — CSS-скобки экранированы удвоением.
"""

from fastapi.responses import HTMLResponse

_STYLE = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ЧатМост</title><link rel="icon" href="/static/brand/favicon.ico" sizes="48x48">
<style>
 body{{margin:0;font-family:-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
 background:#f2f4f8;color:#1c2733;display:flex;align-items:center;justify-content:center;
 height:100vh;font-size:14px}}
 .card{{background:#fff;border:1px solid #dfe3ea;border-radius:14px;max-width:440px;
 padding:24px;box-shadow:0 1px 3px rgba(20,30,50,.05)}}
 .mark{{height:44px;width:auto;flex:none;display:block;margin-bottom:14px}}
 h1{{font-size:17px;margin:0 0 8px}}
 p{{color:#66707d;line-height:1.5;margin:6px 0}}
</style></head><body><div class="card">
<img class="mark" src="/static/brand/logo-128x69.png" alt="" aria-hidden="true">
<h1>{title}</h1><p>{body}</p></div></body></html>"""


def page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(status_code=status_code, content=_STYLE.format(title=title, body=body))
