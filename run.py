#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Drupalport auf Python im Zuge der Entwicklung von Pycroft.
Erstellt am 02.03.2014 von Dominik Pataky pataky@wh2.tu-dresden.de
"""

import io

from flask import Flask, render_template, request, redirect, \
    url_for, flash, send_file, session
from flask.ext.login import LoginManager, current_user, login_user, \
    logout_user
from flask.ext.babel import Babel, gettext
from flask.ext.flatpages import FlatPages
from sqlalchemy.exc import OperationalError
from ldap import SERVER_DOWN
from markdown import Markdown

from blueprints import bp_usersuite, bp_pages
from config import languages, busstops
from forms import flash_formerrors, LoginForm
from utils import get_bustimes
from utils.database_utils import query_userinfo, query_trafficdata
from utils.exceptions import UserNotFound, PasswordInvalid, DBQueryEmpty
from utils.graph_utils import make_trafficgraph
from utils.ldap_utils import User, authenticate


app = Flask(__name__)
app.config.from_pyfile('settings.py')
login_manager = LoginManager()
login_manager.init_app(app)
babel = Babel(app)
pages = FlatPages(app)

# Blueprints
app.register_blueprint(bp_usersuite)
app.register_blueprint(bp_pages)

# global jinja variable containing data for the gauge
app.jinja_env.globals.update(
    traffic=(lambda l: {
        "credit": l[3],
        "exhausted": l[1]+l[2]
    })(query_trafficdata("141.30.223.106")['history'][-1])
)


def errorpage(e):
    if e.code in (404,):
        flash(gettext(u"Seite nicht gefunden!"), "warning")
    elif e.code in (401, 403):
        flash(gettext(u"Sie haben nicht die notwendigen Rechte um die Seite zu sehen!"), "warning")
    else:
        flash(gettext(u"Es ist ein Fehler aufgetreten!"), "error")
    return redirect(url_for("index"))
app.register_error_handler(401, errorpage)
app.register_error_handler(403, errorpage)
app.register_error_handler(404, errorpage)


def safe_markdown(text):
    md = Markdown(safe_mode='escape')
    return md.convert(text)


@app.errorhandler(OperationalError)
def exceptionhandler_sql(ex):
    """Handles global MySQL errors (server down).
    """
    flash(u"Connection to SQL server could not be established!", "error")
    return redirect(url_for('index'))


@app.errorhandler(SERVER_DOWN)
def exceptionhandler_ldap(ex):
    """Handles global LDAP SERVER_DOWN exceptions.
    The session must be reset, because if the user is logged in and the server
    fails during his session, it would cause a redirect loop.
    This also resets the language choice, btw.

    The alternative would be a try-except catch block in load_user, but login
    also needs a handler.
    """
    session.clear()
    flash(u"Connection to LDAP server could not be established!", "error")
    return redirect(url_for('index'))


@login_manager.user_loader
def load_user(username):
    """Loads a User object from/into the session at every request
    """
    return User.get(username)


@babel.localeselector
def babel_selector():
    """Tries to get the language setting from the current session cookie.
    If this fails (if it is not set) it first checks if a language was
    submitted as an argument ('/page?lang=de') and if not, the best matching
    language out of the header accept-language is chosen and set.
    """
    lang = session.get('lang')

    if 'lang' in request.args and request.args['lang'] in ('de', 'en'):
        session['lang'] = request.args['lang']
    elif not lang:
        session['lang'] = request.accept_languages.best_match(languages.keys())

    return session.get('lang')


@app.route('/index.php')
@app.route('/')
def index():
    """Get all markdown files from 'news/', parse them and put
    them in a list for the template.

    The format is like this (Pelican compatible):

        Title: ABC
        Author: userX
        Date: 1970-01-01
        [Type: alert]

        Message

    The type field does not need to be used. If you use it, check what types
    are available. For now, it's only 'alert' which colors the news entry red.
    """
    lang = session.get('lang', 'de')

    pages.reload()
    articles = (p for p in pages if p.path.startswith(lang + u'/news/'))
    latest = sorted(articles, key=lambda a: a.meta['date'], reverse=True)

    return render_template("index.html", articles=latest)


@app.route("/login", methods=['GET', 'POST'])
def login():
    """Login page for users
    """
    form = LoginForm()

    if form.validate_on_submit():
        username = form.username.data
        password = form.password.data

        try:
            user = authenticate(username, password)
        except UserNotFound:
            flash(gettext(u"Nutzer nicht gefunden!"), "error")
        except PasswordInvalid:
            flash(gettext(u"Passwort war inkorrekt!"), "error")
        else:
            if isinstance(user, User):
                login_user(user)
    elif form.is_submitted():
        flash_formerrors(form)

    if current_user.is_authenticated():
        return redirect(url_for('usersuite.usersuite'))

    return render_template('login.html', form=form)


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("index"))


@app.route("/language/<string:lang>")
def set_language(lang='de'):
    """Set the session language via URL
    """
    session['lang'] = lang
    return redirect(request.referrer)


@app.route("/usertraffic")
def usertraffic():
    """For anonymous users with a valid IP
    """
    try:
        trafficdata = query_trafficdata(request.remote_addr)
    except DBQueryEmpty:
        flash(gettext(u"Deine IP gehört nicht zum Wohnheim!"), "error")
        return redirect(url_for('index'))

    return render_template("usertraffic.html", usertraffic=trafficdata)


@app.route("/traffic.png")
def trafficpng():
    """Create a traffic chart as png binary file and return the binary
    object to the client.

    If the user is not logged in, try to create a graph for the remote IP.
    Fails, if the IP was not recognized.
    """
    if current_user.is_anonymous():
        ip = request.remote_addr
    else:
        userinfo = query_userinfo(current_user.uid)
        ip = userinfo['ip']

    try:
        trafficdata = query_trafficdata(ip)
    except DBQueryEmpty:
        flash(gettext(u"Es gab einen Fehler bei der Datenbankanfrage!"), "error")
        return redirect(url_for('index'))

    traffic_chart = make_trafficgraph(trafficdata)

    # todo fix png export, use svg or include svg directly in html
    # pygals render_to_png IS BROKEN
    # proof of concept: Just add some stuff to a bar_chart = pygal.Bar()
    # then compare the outputs of render_to_file and render_to_png
    # the first (svg) will work just fine, but not the second (png)
    # alternative: directly import into the html, there is no need for a file
    return send_file(io.BytesIO(traffic_chart.render_to_png()), "image/png")


@app.route("/bustimes")
@app.route("/bustimes/<string:stopname>")
def bustimes(stopname=None):
    """Queries the VVO-Online widget for the given stop.
    If no specific stop is given in the URL, it will query all
    stops set up in the config.
    """
    data = {}

    if stopname:
        # Only one stop requested
        data[stopname] = get_bustimes(stopname)
    else:
        # General output page
        for stop in busstops:
            data[stop] = get_bustimes(stop, 4)

    return render_template('bustimes.html', times=data, stopname=stopname)


if __name__ == "__main__":
    app.run(debug=True, host="localhost")
