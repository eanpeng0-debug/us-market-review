# -*- coding: utf-8 -*-
"""
app.py — USAR 论文模式监控卡片的独立 Flask 入口。

注意:本仓库为全新空仓库,不存在既有 dashboard 的 app.py。
按规格"只新增一个路由 /usar、不动其他路由"的精神,这里仅暴露 /usar。
USAR 卡片自带数据源 (usar_config.json + usar_live.json),与 v3/v4 引擎、
portfolio*.json 完全解耦。

启动:  python app.py   →  http://127.0.0.1:5000/usar
"""

from flask import Flask, render_template

import usar_monitor

app = Flask(__name__)


@app.route("/usar")
def usar_card():
    state = usar_monitor.build_card_state()
    return render_template("usar_card.html", s=state)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
