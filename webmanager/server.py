import json
import os
import sys
sys.path.insert(0, "../")

from flask import Flask, jsonify, send_from_directory, request, render_template

try:
    from webmanager.helpfile import help_file, buildings
    from webmanager.utils import (
        DataReader, BotManager, MapBuilder, BuildingTemplateManager,
        TroopTemplateManager, OffensiveTemplateManager, LogReader
    )
    from webmanager.stats import StatsBuilder
except ImportError:
    from helpfile import help_file, buildings
    from utils import (
        DataReader, BotManager, MapBuilder, BuildingTemplateManager,
        TroopTemplateManager, OffensiveTemplateManager, LogReader
    )
    from stats import StatsBuilder

bm = BotManager()

app = Flask(__name__)
app.config["DEBUG"] = True


def positive_int_arg(name, default, max_value=None):
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    value = max(1, value)
    if max_value:
        value = min(value, max_value)
    return value


def paginate_items(items, page=None, per_page=None):
    page = page or positive_int_arg("page", 1)
    per_page = per_page or positive_int_arg("per_page", 25, max_value=200)
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    end = start + per_page
    return items[start:end], {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": page - 1,
        "next_page": page + 1,
        "start": start + 1 if total else 0,
        "end": min(end, total),
    }


def pre_process_bool(key, value, village_id=None):
    if village_id:
        if value:
            return '<button class="btn btn-sm btn-block btn-success" data-village-id="%s" data-type-option="%s" data-type="toggle">Enabled</button>' % (
            village_id, key)
        else:
            return '<button class="btn btn-sm btn-block btn-danger" data-village-id="%s" data-type-option="%s" data-type="toggle">Disabled</button>' % (
            village_id, key)
    if value:
        return '<button class="btn btn-sm btn-block btn-success" data-type-option="%s" data-type="toggle">Enabled</button>' % key
    else:
        return '<button class="btn btn-sm btn-block btn-danger" data-type-option="%s" data-type="toggle">Disabled</button>' % key


def preprocess_select(key, value, templates, village_id=None):
    output = '<select data-type-option="%s" data-type="select" class="form-control">' % key
    if village_id:
        output = '<select data-type-option="%s" data-village-id="%s" data-type="select" class="form-control">' % (
        key, village_id)

    for template in DataReader.template_grab(templates):
        output += '<option value="%s" %s>%s</option>' % (template, 'selected' if template == value else '', template)
    output += '</select>'
    return output


def pre_process_string(key, value, village_id=None):
    templates = {
        'units.default': 'templates.troops',
        'village.units': 'templates.troops',
        'building.default': 'templates.builder',
        'village_template.units': 'templates.troops',
        'village.building': 'templates.builder',
        'village_template.building': 'templates.builder'
    }
    if key in templates:
        return preprocess_select(key, value, templates[key], village_id)
    if village_id:
        return '<input type="text" class="form-control" data-village-id="%s" data-type="text" value="%s" data-type-option="%s" />' % (
        village_id, value, key)
    else:
        return '<input type="text" class="form-control" data-type="text" value="%s" data-type-option="%s" />' % (
            value, key)


def pre_process_number(key, value, village_id=None):
    if village_id:
        return '<input type="number" data-type="number" class="form-control" data-village-id="%s" value="%s" data-type-option="%s" />' % (
        village_id, value, key)
    return '<input type="number" data-type="number" class="form-control" value="%s" data-type-option="%s" />' % (
    value, key)


def pre_process_list(key, value, village_id=None):
    if village_id:
        return '<input type="text" data-type="list" class="form-control" data-village-id="%s" value="%s" data-type-option="%s" />' % (
        village_id, ', '.join(value), key)
    return '<input type="number" data-type="list" class="form-control" value="%s" data-type-option="%s" />' % (
    ', '.join(value), key)


def fancy(key):
    name = key
    if '.' in name:
        name = name.split('.')[1]
    name = name[0].upper() + name[1:]
    out = '<hr /><strong>%s</strong>' % name
    help_txt = None
    help_key = key
    help_key = help_key.replace('village_template', 'village')
    if help_key in help_file:
        help_txt = help_file[help_key]
    if help_txt:
        out += '<br /><i>%s</i>' % help_txt
    return out


def pre_process_config():
    # TODO get generic config
    config = sync()['config']
    to_hide = ["build", "villages"]
    sections = {}
    for section in config:
        if section in to_hide:
            continue
        config_data = ""
        for parameter in config[section]:
            value = config[section][parameter]
            kvp = "%s.%s" % (section, parameter)
            if type(value) == bool:
                config_data += '%s %s' % (fancy(kvp), pre_process_bool(kvp, value))
            if type(value) == str:
                config_data += '%s %s' % (fancy(kvp), pre_process_string(kvp, value))
            if type(value) == list:
                config_data += '%s %s' % (fancy(kvp), pre_process_list(kvp, value))
            if type(value) == int or type(value) == float:
                config_data += '%s %s' % (fancy(kvp), pre_process_number(kvp, value))
        sections[section] = config_data
    return sections


def pre_process_village_config(village_id):
    config = sync()['config']['villages']
    if village_id in config:
        config = config[village_id]
    else:
        config = config[config.keys()[0]]
    config_data = ""
    for parameter in config:
        value = config[parameter]
        kvp = "village.%s" % parameter
        if type(value) == bool:
            config_data += '%s %s' % (fancy(kvp), pre_process_bool(kvp, value, village_id))
        if type(value) == str:
            config_data += '%s %s' % (fancy(kvp), pre_process_string(kvp, value, village_id))
        if type(value) == list:
            config_data += '%s %s' % (fancy(kvp), pre_process_list(kvp, value, village_id))
        if type(value) == int or type(value) == float:
            config_data += '%s %s' % (fancy(kvp), pre_process_number(kvp, value, village_id))
    return config_data


def sync():
    reports = DataReader.cache_grab("reports")
    villages = DataReader.cache_grab("villages")
    attacks = DataReader.cache_grab("farms")
    legacy_attacks = DataReader.cache_grab("attacks")
    for key, value in legacy_attacks.items():
        attacks.setdefault(key, value)
    config = DataReader.config_grab()
    managed = DataReader.cache_grab("managed")
    bot_state = bm.status()
    bot_status = bool(bot_state.get("running"))

    sort_reports = {
        key: value
        for key, value in sorted(
            reports.items(),
            key=lambda item: int(item[0]),
            reverse=True,
        )
    }

    out_struct = {
        "attacks": attacks,
        "farms": attacks,
        "villages": villages,
        "config": config,
        "reports": sort_reports,
        "bot": managed,
        "status": bot_status,
        "bot_state": bot_state
    }
    return out_struct


def stats_sync():
    farms = DataReader.cache_grab("farms")
    legacy_attacks = DataReader.cache_grab("attacks")
    for key, value in legacy_attacks.items():
        farms.setdefault(key, value)
    config = DataReader.config_grab()
    return StatsBuilder.build(
        DataReader.cache_grab("reports"),
        farms,
        DataReader.cache_grab("managed"),
        config,
    )


@app.route('/api/get', methods=['GET'])
def get_vars():
    return jsonify(sync())


@app.route('/api/stats', methods=['GET'])
def get_stats_api():
    return jsonify(stats_sync())


@app.route('/bot/start')
def start_bot():
    return jsonify(bm.start())


@app.route('/bot/stop')
def stop_bot():
    return jsonify(bm.stop())


@app.route('/bot/status')
def bot_status():
    return jsonify(bm.status())


@app.route('/bot/session', methods=['POST'])
def set_bot_session():
    raw = request.form.get('raw', '') or (request.get_json(silent=True) or {}).get('raw', '')
    if not DataReader.set_session_cookies(raw):
        return jsonify({"ok": False, "error": "No valid cookies parsed"}), 400
    restarted = bm.restart()
    return jsonify({"ok": True, "restarted": restarted})


@app.route('/config', methods=['GET'])
def get_config():
    return render_template('config.html', data=sync(), config=pre_process_config(), helpfile=help_file)


@app.route('/village', methods=['GET'])
def get_village_config():
    data = sync()
    vid = request.args.get("id", None)
    return render_template('village.html', data=data, config=pre_process_village_config(village_id=vid),
                           current_select=vid, helpfile=help_file)


@app.route('/map', methods=['GET'])
def get_map():
    sync_data = sync()
    center_id = request.args.get("center", None)
    center = next(iter(sync_data['bot'])) if not center_id else center_id
    map_data = json.dumps(MapBuilder.build(sync_data['villages'], current_village=center, size=15))
    return render_template('map.html', data=sync_data, map=map_data)


@app.route('/villages', methods=['GET'])
def get_village_overview():
    return render_template('villages.html', data=sync())


@app.route('/stats', methods=['GET'])
def get_stats():
    return render_template('stats.html', data=sync(), stats=stats_sync())


@app.route('/logs', methods=['GET'])
def get_logs():
    data = sync()
    logs = LogReader.from_config(data["config"])
    entries, pagination = paginate_items(
        logs["entries"],
        per_page=positive_int_arg("per_page", 50, max_value=200),
    )
    logs["total_entries"] = len(logs["entries"])
    logs["entries"] = entries
    return render_template('logs.html', data=data, logs=logs, pagination=pagination)


@app.route('/building_templates', methods=['GET', 'POST'])
def get_building_templates():
    if request.form.get('new', None):
        plain = os.path.basename(request.form.get('new'))
        if not plain.endswith('.txt'):
            plain = "%s.txt" % plain
        tempfile = BuildingTemplateManager.template_path(plain)
        if not os.path.exists(tempfile):
            with open(tempfile, 'w') as ouf:
                ouf.write("")
    selected = request.args.get('t', None)
    return render_template('templates.html',
                           templates=BuildingTemplateManager.template_cache_list(),
                           selected=selected,
                           buildings=buildings)


@app.route('/building_templates/save', methods=['POST'])
def save_building_template():
    payload = request.get_json(silent=True) or {}
    template = payload.get("template")
    rows = payload.get("rows", [])
    if not template:
        return jsonify({"ok": False, "error": "Missing template"}), 400
    saved = BuildingTemplateManager.save_template(template, rows)
    return jsonify({"ok": True, "rows": saved})


@app.route('/troop_templates', methods=['GET', 'POST'])
def get_troop_templates():
    if request.form.get('new', None):
        plain = os.path.basename(request.form.get('new'))
        if not plain.endswith('.txt'):
            plain = "%s.txt" % plain
        path = TroopTemplateManager.template_path(plain)
        if not os.path.exists(path):
            with open(path, 'w') as output_file:
                output_file.write("[]\n")
    selected = request.args.get('t', None)
    return render_template(
        'troop_templates.html',
        templates=TroopTemplateManager.template_cache_list(),
        selected=selected,
        buildings=buildings
    )


@app.route('/troop_templates/save', methods=['POST'])
def save_troop_template():
    payload = request.get_json(silent=True) or {}
    template = payload.get("template")
    rows = payload.get("rows", [])
    if not template:
        return jsonify({"ok": False, "error": "Missing template"}), 400
    saved = TroopTemplateManager.save_template(template, rows)
    return jsonify({"ok": True, "rows": len(saved)})


@app.route('/offensive_templates', methods=['GET', 'POST'])
def get_offensive_templates():
    if request.form.get('new', None):
        plain = os.path.basename(request.form.get('new'))
        if not plain.endswith('.txt'):
            plain = "%s.txt" % plain
        path = OffensiveTemplateManager.template_path(plain)
        if not os.path.exists(path):
            with open(path, 'w') as output_file:
                json.dump({"village": "any", "groups": []}, output_file, indent=2)
                output_file.write("\n")
    selected = request.args.get('t', None)
    return render_template(
        'offensive_templates.html',
        templates=OffensiveTemplateManager.template_cache_list(),
        selected=selected
    )


@app.route('/offensive_templates/save', methods=['POST'])
def save_offensive_template():
    payload = request.get_json(silent=True) or {}
    template = payload.get("template")
    rows = payload.get("rows", [])
    village = payload.get("village", "any")
    if not template:
        return jsonify({"ok": False, "error": "Missing template"}), 400
    saved = OffensiveTemplateManager.save_template(template, village, rows)
    return jsonify({"ok": True, "rows": len(saved["groups"])})


@app.route('/', methods=['GET'])
def get_home():
    session = DataReader.get_session()
    data = sync()
    reports = [
        {
            "id": report_id,
            "type": report_data.get("type", ""),
            "data": report_data,
        }
        for report_id, report_data in data["reports"].items()
    ]
    reports, pagination = paginate_items(reports)
    return render_template(
        'bot.html',
        data=data,
        session=session,
        reports=reports,
        pagination=pagination,
    )


@app.route('/app/js', methods=['GET'])
def get_js():
    urlpath = os.path.join(os.path.dirname(__file__), "public")
    return send_from_directory(urlpath, "js.v2.js")


@app.route('/app/config/set', methods=['GET'])
def config_set():
    vid = request.args.get("village_id", None)
    if not vid:
        DataReader.config_set(parameter=request.args.get("parameter"), value=request.args.get("value", None))
    else:
        param = request.args.get("parameter")
        if param.startswith("village."):
            param = param.replace("village.", "")
        DataReader.village_config_set(village_id=vid, parameter=param, value=request.args.get("value", None))

    return jsonify(sync())


if __name__ == "__main__":
    if len(sys.argv) > 1:
        app.run(host="localhost", port=sys.argv[1])
    else:
        app.run()
